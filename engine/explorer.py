"""Journey-aware exploration: the core state-mapping loop.

Pending actions are scheduled across user journeys instead of exhausting one
deep branch. Meaningful local continuations (modals/forms/wizards) remain
together, while root navigation journeys receive fair coverage.
Each step navigates to its source state (replaying the stored path when
needed), performs the action, observes the result, and either merges into a
known state (identity or URL dedup) or registers a new node and enqueues its
own ranked, safety-filtered actions on top of the stack. Stops when the
stack drains or any budget is exhausted.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine import identity
from engine.browser.actions import (
    click_interactable,
    click_selector,
    dismiss_cookie_banner,
    validate_interactable,
)
from engine.browser.autofill import autofill_auth_form
from engine.browser.session import BrowserSession
from engine.browser.snapshot import stabilize
from engine.capture import (
    build_state_row,
    new_id,
    observe_page,
    persist_state,
    with_auth_context,
)
from engine.classify import StateAnalysis, analyze_state
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.events import ActionOutcome, EventType
from engine.identity import normalize_url
from engine.organize import heuristic_name, infer_page_role
from engine.ranking import (
    ActionCandidate,
    SurfaceFamily,
    detect_surface_families,
    infer_url_family,
    is_auth_entry,
    score_action,
)
from engine.safety import evaluate_action, is_same_origin
from engine.schemas import (
    ActionStep,
    AuthContext,
    AuthMode,
    BudgetConfig,
    Credentials,
    Interactable,
    Observation,
    PageRole,
    RunConfig,
    StateType,
)
from engine.storage import LocalStorage

# Stop when this many consecutive actions produce no new state (novelty collapse).
NOVELTY_PATIENCE = 30

_TERMINAL_TYPES = frozenset(
    {StateType.EXTERNAL, StateType.DEAD_END, StateType.RISKY_TERMINAL}
)

# Gate states: discovered and recorded but not expanded by default.
# The auth_gate_hook decides whether to expand them after user interaction.
_GATE_TYPES = frozenset({StateType.AUTH_WALL})

# Callback invoked when an auth-wall state is reached.
# Receives (state_id, url); returns (decision, credentials_or_None).
# decision: "resume" | "skip"
AuthGateHook = Callable[[str, str], Awaitable[tuple[str, "Credentials | None"]]]


@dataclass
class ExplorerEvent:
    kind: str
    message: str
    data: dict = field(default_factory=dict)


EventSink = Callable[[ExplorerEvent], None]


@dataclass
class StateMeta:
    """In-memory record of a registered state (what the loop needs)."""

    id: str
    index: int
    url: str
    url_normalized: str
    depth: int
    path: list[ActionStep]
    state_type: StateType
    parent_state_id: str | None = None
    interactables: list[Interactable] = field(default_factory=list)
    # item_ids of the chosen representatives (post sibling-collapse) and of
    # the items the safety layer blocked -- used to derive surface statuses.
    representative_ids: set[str] = field(default_factory=set)
    blocked_ids: set[str] = field(default_factory=set)
    route_family: str | None = None
    auth_context: AuthContext = AuthContext.UNKNOWN
    page_depth: int = 0
    substate_depth: int = 0
    journey_key: str = "root"
    family: dict | None = None
    page_role: PageRole = PageRole.FLOW_STEP
    display_label: str = ""
    # Exact canonical state expected after closing/cancelling a local surface.
    # This is intentionally separate from the organizational page parent.
    return_state_id: str | None = None
    nav_capabilities: list[dict] = field(default_factory=list)
    surface_families: list[dict] = field(default_factory=list)


@dataclass
class TransitionCapability:
    """One visible source-local control that can support a directed edge."""

    source_id: str
    capability_id: str
    control_key: str
    item: Interactable
    target_hint: str | None = None
    global_key: str | None = None


@dataclass
class PendingAction:
    from_state: StateMeta
    candidate: ActionCandidate
    journey_key: str = "root"
    phase: int = 1
    sequence: int = 0


class Budget:
    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self.actions = 0
        self._started = time.monotonic()

    def note_action(self) -> None:
        self.actions += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    def stop_reason(self, state_count: int, actions_since_new: int) -> str | None:
        if self.actions >= self._config.max_actions:
            return "max_actions"
        if state_count >= self._config.max_states:
            return "max_states"
        if self.elapsed_seconds >= self._config.max_wall_seconds:
            return "max_wall_seconds"
        if actions_since_new >= NOVELTY_PATIENCE:
            return "novelty_exhausted"
        return None


class Frontier:
    """Deterministic fair scheduler across root-level user journeys."""

    def __init__(self) -> None:
        self._items: list[PendingAction] = []
        self._journey_pops: dict[str, int] = {}
        self._sequence = 0

    def push(self, item: PendingAction) -> None:
        item.sequence = self._sequence
        self._sequence += 1
        self._items.append(item)

    def pop(self) -> PendingAction:
        def priority(item: PendingAction) -> tuple:
            local_continuation = item.from_state.state_type in {
                StateType.MODAL,
                StateType.DROPDOWN,
                StateType.FORM,
                StateType.WIZARD_STEP,
            }
            return (
                item.phase,
                0 if local_continuation else 1,
                self._journey_pops.get(item.journey_key, 0),
                -item.candidate.score,
                item.from_state.page_depth,
                item.sequence,
            )

        index = min(range(len(self._items)), key=lambda i: priority(self._items[i]))
        item = self._items.pop(index)
        self._journey_pops[item.journey_key] = (
            self._journey_pops.get(item.journey_key, 0) + 1
        )
        return item

    def __len__(self) -> int:
        return len(self._items)


_AUTH_SUCCESS = re.compile(
    r"\b(log\s*out|sign\s*out|my\s+account|profile|dashboard)\b", re.I
)
_AUTH_PATH = re.compile(r"/(log-?in|sign-?in|sign-?up|register|auth)(/|$)", re.I)
_AUTH_RETURN_PARAMS = {
    "after",
    "continue",
    "next",
    "redirect",
    "redirect_uri",
    "return",
    "return_to",
    "returnurl",
}

_PERIPHERAL_ACTION = re.compile(
    r"\b("
    r"about|advertis(e|ing)|brand|careers?|community\s+guidelines|copyright|"
    r"cookie|developers?|feedback|help|imprint|jobs?|legal|press|privacy|"
    r"policy|report(\s+history)?|safety|terms?"
    r")\b",
    re.I,
)
_GENERIC_EXPANSION = re.compile(
    r"\b(show|see|view|load)\s+more\b|\bmore\b|\bexpand\b", re.I
)


def _canonical_scope_host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _effective_scope_url(requested_url: str, resolved_url: str) -> str:
    """Use the post-redirect URL for canonical same-site redirects.

    This keeps youtube.com -> www.youtube.com and http -> https redirects in
    scope without treating an arbitrary cross-domain redirect as the app.
    """
    requested = urlsplit(requested_url)
    resolved = urlsplit(resolved_url)
    if requested.scheme == resolved.scheme == "file":
        return resolved_url
    if (
        requested.scheme in {"http", "https"}
        and resolved.scheme in {"http", "https"}
        and _canonical_scope_host(requested_url) == _canonical_scope_host(resolved_url)
    ):
        return resolved_url
    return requested_url


def _journey_slug(label: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return value[:48] or "other"


def _auth_surface_url(url: str) -> str | None:
    parts = urlsplit(normalize_url(url))
    if not _AUTH_PATH.search(parts.path):
        return None
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in _AUTH_RETURN_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


class Explorer:
    """Runs one exploration and persists the resulting state graph."""

    def __init__(
        self,
        settings: Settings,
        config: RunConfig,
        *,
        on_event: EventSink | None = None,
        auth_gate_hook: AuthGateHook | None = None,
        credentials: Credentials | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._emit: EventSink = on_event or (lambda event: None)
        self._auth_gate_hook = auth_gate_hook
        self._credentials = credentials
        self._auth_mode = config.authentication.mode

    async def run(self, url: str, *, run_id: str | None = None) -> str:
        """Explore `url` until budgets exhaust; returns the run id.

        A `run_id` may be supplied so a caller (e.g. the API) can register
        and return the id before exploration starts.
        """
        self._run_id = run_id or new_id()
        self._requested_url = url
        self._root_url = url
        self._budget = Budget(self._config.budgets)
        self._frontier = Frontier()
        self._identity = identity.IdentityIndex()
        self._states: dict[str, StateMeta] = {}
        self._visited_urls: set[str] = set()
        self._auth_context = AuthContext.GUEST
        # First state registered for each normalized URL (inferred-edge target).
        self._url_to_state: dict[tuple[AuthContext, str], str] = {}
        self._auth_surface_to_state: dict[tuple[AuthContext, str], str] = {}
        # Structure-confirmed representatives and bounded sampling per
        # content-like route family inferred from repeated link cohorts.
        self._family_variants: dict[str, dict[tuple, str]] = {}
        self._family_attempts: dict[str, int] = {}
        self._family_sampled_urls: dict[str, list[str]] = {}
        self._family_skipped: dict[str, int] = {}
        self._family_info: dict[str, dict] = {}
        self._edges_done: set[tuple[str, str]] = set()
        self._capabilities: dict[tuple[str, str], TransitionCapability] = {}
        self._capability_by_item: dict[tuple[str, str], str] = {}
        self._waiting_by_target: dict[
            tuple[AuthContext, str], set[tuple[str, str]]
        ] = defaultdict(set)
        self._global_capabilities: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._global_targets: dict[str, str] = {}
        self._ambiguous_global_targets: set[str] = set()
        self._tab_capabilities: dict[
            tuple[str, str], set[tuple[str, str]]
        ] = defaultdict(set)
        self._tab_targets: dict[tuple[str, str], str] = {}
        self._edge_records: dict[str, dict] = {}
        self._validated_history: set[tuple[str, str]] = set()
        # (state_id, item_id) -> terminal surface status for items we acted on.
        self._item_outcome: dict[tuple[str, str], str] = {}
        self._current_state_id: str | None = None
        self._actions_since_new = 0
        self._stats: dict = {
            "states": 0,
            "edges": 0,
            "inferred_edges": 0,
            "dedup_hits": 0,
            "noop_actions": 0,
            "failed_actions": 0,
            "actions_denied": 0,
            "family_dedup_hits": 0,
            "family_urls_skipped": 0,
            "restoration_checks": 0,
            "global_navigation_edges": 0,
            "reversible_edges": 0,
            "surface_pending_items": 0,
            "frontier_actions": 0,
        }

        engine = create_db_engine(self._settings.database_url)
        await init_db(engine)
        self._sessions = create_session_factory(engine)
        self._store = LocalStorage(self._settings.data_dir)

        async with self._sessions() as session:
            session.add(
                db.Run(
                    id=self._run_id, url=url, status="running", config=self._config.model_dump()
                )
            )
            await session.commit()

        try:
            async with BrowserSession(self._config.browser) as browser:
                page = await browser.new_page()
                await self._explore(page, url)
        except Exception as exc:
            await self._finish_run("failed", error=str(exc))
            await engine.dispose()
            raise

        await self._finish_run("done")
        await engine.dispose()
        return self._run_id

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _explore(self, page: Page, url: str) -> None:
        self._emit(
            ExplorerEvent(
                EventType.RUN_STARTED,
                f"Exploring {url}",
                {
                    "url": url,
                    "config": {
                        "max_states": self._config.budgets.max_states,
                        "max_actions": self._config.budgets.max_actions,
                        "max_depth": self._config.budgets.max_depth,
                        "max_wall_seconds": self._config.budgets.max_wall_seconds,
                        "auth_mode": self._auth_mode.value,
                    },
                    "counters": self._counters(),
                },
            )
        )
        await page.goto(url)
        await dismiss_cookie_banner(page)
        self._root_url = _effective_scope_url(url, page.url)

        observation = await observe_page(
            page, self._config, auth_context=self._auth_context
        )
        root = await self._register_state(
            observation,
            depth=0,
            path=[ActionStep(kind="goto", url=page.url)],
            enqueue_actions=self._auth_mode == AuthMode.GUEST,
        )
        self._current_state_id = root.id

        if root.state_type == StateType.AUTH_WALL:
            if self._auth_mode == AuthMode.LOGIN:
                post_auth = await self._handle_auth_wall(page, root)
                if post_auth is not None:
                    await self._add_user_auth_edge(root, post_auth)
        elif self._auth_mode == AuthMode.LOGIN:
            self._enqueue_auth_discovery(root, analyze_state(observation, base_url=self._root_url))

        self._emit_frontier()

        while len(self._frontier):
            reason = self._budget.stop_reason(len(self._states), self._actions_since_new)
            if reason:
                self._stats["stop_reason"] = reason
                break
            await self._execute(page, self._frontier.pop())
            self._emit_frontier()
        else:
            self._stats["stop_reason"] = "frontier_exhausted"

    def _counters(self) -> dict:
        """UI-friendly snapshot of live progress counters."""
        return {
            "states": self._stats["states"],
            "edges": self._stats["edges"],
            "inferred_edges": self._stats["inferred_edges"],
            "denied": self._stats["actions_denied"],
            "deduped": self._stats["dedup_hits"],
            "noop": self._stats["noop_actions"],
            "failed": self._stats["failed_actions"],
            "actions_performed": self._budget.actions,
            "frontier_size": len(self._frontier),
            "surface_pending": self._stats.get("surface_pending_items", 0),
        }

    def _emit_frontier(self) -> None:
        self._emit(
            ExplorerEvent(
                EventType.FRONTIER_UPDATED,
                "",
                {
                    "frontier_size": len(self._frontier),
                    "pending_actions": len(self._frontier),
                    "counters": self._counters(),
                },
            )
        )

    def _emit_action_finished(
        self, outcome: ActionOutcome, message: str, **extra: object
    ) -> None:
        self._emit(
            ExplorerEvent(
                EventType.ACTION_FINISHED,
                message,
                {"outcome": outcome.value, **extra},
            )
        )

    @staticmethod
    def _is_global_chrome_item(item: Interactable) -> bool:
        return bool(item.in_nav or item.region in {"nav", "header", "aside"})

    @staticmethod
    def _is_peripheral_action(item: Interactable) -> bool:
        if item.region == "footer":
            return True
        href_path = urlsplit(item.href).path if item.href else ""
        return bool(_PERIPHERAL_ACTION.search(f"{item.label} {href_path}"))

    @staticmethod
    def _is_generic_expansion(item: Interactable) -> bool:
        if item.href:
            return False
        return bool(_GENERIC_EXPANSION.search(item.label))

    def _known_target_id(self, auth_context: AuthContext, target_norm: str) -> str | None:
        target_id = self._url_to_state.get((auth_context, target_norm))
        if target_id is None:
            target_id = self._url_to_state.get(target_norm)  # legacy unit fixtures
        return target_id

    def _should_materialize_inferred_edge(
        self, source: StateMeta, item: Interactable
    ) -> bool:
        """Keep inferred edges useful without turning shared chrome into a sitemap.

        Root/global hub navigation is valuable. The same sidebar/header control
        repeated from every later state is capability evidence, not a primary
        product transition for V1.
        """
        if not self._is_global_chrome_item(item):
            return True
        return source.depth == 0 or source.page_role == PageRole.HOME

    def _inferred_target(self, source: StateMeta, item: Interactable) -> str | None:
        """If `item` is a plain same-origin link to an already-known URL state,
        return that state's id so we can record the edge without clicking."""
        if not self._should_materialize_inferred_edge(source, item):
            return None
        if item.tag != "a" or not item.href:
            return None
        href = item.href
        if not href.startswith(("http://", "https://", "file://")):
            return None
        if not is_same_origin(href, self._root_url):
            return None
        target_norm = normalize_url(href)
        if target_norm == source.url_normalized:
            return None
        target_id = self._known_target_id(source.auth_context, target_norm)
        if target_id is None or target_id == source.id:
            return None
        return target_id

    @staticmethod
    def _stable_hash(*parts: str | None, length: int = 20) -> str:
        basis = "|".join((part or "").strip().lower() for part in parts)
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:length]

    def _control_key(self, item: Interactable) -> str:
        if item.control_key:
            return item.control_key
        return self._stable_hash(
            item.kind,
            item.region,
            item.label,
            item.href,
            identity.strip_positional_selector(item.selector),
            item.container_key,
            length=16,
        )

    def _capability_id(self, source: StateMeta, item: Interactable) -> str:
        return self._stable_hash(
            source.id,
            item.item_id,
            self._control_key(item),
            length=20,
        )

    def _target_hint(self, item: Interactable) -> str | None:
        href = item.href
        if not href or not href.startswith(("http://", "https://", "file://")):
            return None
        if not is_same_origin(href, self._root_url):
            return None
        return normalize_url(href)

    def _global_key_for(
        self, auth_context: AuthContext, item: Interactable, target_hint: str | None
    ) -> str | None:
        if not self._is_global_chrome_item(item):
            return None
        origin = urlsplit(self._root_url)
        return self._stable_hash(
            origin.scheme,
            origin.netloc,
            auth_context.value,
            item.container_key,
            item.kind,
            item.label,
            target_hint,
            identity.strip_positional_selector(item.selector),
            length=24,
        )

    def _global_key(
        self, source: StateMeta, item: Interactable, target_hint: str | None
    ) -> str | None:
        return self._global_key_for(source.auth_context, item, target_hint)

    def _nav_capabilities_for(
        self, auth_context: AuthContext, items: list[Interactable]
    ) -> list[dict]:
        """Compact reusable navigation evidence stored on the source state.

        These capabilities are context for the state machine/LLM. They are not
        automatically materialized as graph edges from every source state.
        """
        capabilities: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not self._is_global_chrome_item(item):
                continue
            if not evaluate_action(item, base_url=self._root_url).allowed:
                continue
            target_hint = self._target_hint(item)
            global_key = self._global_key_for(auth_context, item, target_hint)
            if global_key is None or global_key in seen:
                continue
            seen.add(global_key)
            capabilities.append(
                {
                    "id": global_key,
                    "label": item.label,
                    "kind": item.kind,
                    "href": item.href,
                    "target_url": target_hint,
                    "target_state_id": (
                        self._known_target_id(auth_context, target_hint)
                        if target_hint is not None
                        else None
                    ),
                    "region": item.region,
                    "control_key": self._control_key(item),
                    "container_key": item.container_key,
                    "surface_item_id": item.item_id or None,
                }
            )
        return capabilities

    @staticmethod
    def _family_payload(family: SurfaceFamily) -> dict:
        return {
            "id": family.family_id,
            "label": family.label,
            "kind": family.kind,
            "pattern": family.pattern,
            "label_source": "heuristic",
            "confidence": 0.85,
            "discovered_count": family.discovered_count,
            "sample_labels": family.sample_labels,
            "sample_urls": family.sample_urls,
        }

    @staticmethod
    def _url_family_payload(family) -> dict:
        return {
            "id": family.family_id,
            "label": family.label,
            "kind": family.kind,
            "pattern": family.pattern,
            "label_source": "heuristic",
            "confidence": 0.8,
            "discovered_count": 1,
            "sample_labels": [],
            "sample_urls": [],
        }

    def _family_runtime_payload(self, pattern: str) -> dict:
        info = dict(self._family_info.get(pattern, {}))
        info["checked_count"] = len(self._family_sampled_urls.get(pattern, []))
        info["represented_count"] = len(self._family_variants.get(pattern, {}))
        info["skipped_count"] = self._family_skipped.get(pattern, 0)
        info["sample_urls"] = list(
            dict.fromkeys(
                [
                    *info.get("sample_urls", []),
                    *self._family_sampled_urls.get(pattern, []),
                ]
            )
        )[:8]
        return info

    def _apply_surface_families(
        self, observation: Observation, analysis: StateAnalysis
    ) -> list[dict]:
        """Register repeated content families visible from a state surface."""
        families = detect_surface_families(observation.interactables)
        if not families:
            return []

        item_to_family: dict[str, SurfaceFamily] = {}
        for family in families:
            payload = self._family_payload(family)
            existing = self._family_info.get(family.pattern, {})
            sample_labels = list(
                dict.fromkeys(
                    [
                        *existing.get("sample_labels", []),
                        *payload.get("sample_labels", []),
                    ]
                )
            )[:8]
            sample_urls = list(
                dict.fromkeys(
                    [
                        *existing.get("sample_urls", []),
                        *payload.get("sample_urls", []),
                    ]
                )
            )[:8]
            self._family_info[family.pattern] = {
                **existing,
                **payload,
                "discovered_count": max(
                    int(existing.get("discovered_count", 0)),
                    family.discovered_count,
                ),
                "sample_labels": sample_labels,
                "sample_urls": sample_urls,
            }
            for item_id in family.item_ids:
                item_to_family[item_id] = family

        first_for_pattern: set[str] = set()
        for candidate in analysis.candidates:
            item_id = candidate.interactable.item_id or candidate.interactable.selector
            family = item_to_family.get(item_id)
            if family is None:
                continue
            if candidate.family_pattern is None:
                candidate.family_pattern = family.pattern
                candidate.family_id = family.family_id
                candidate.family_label = family.label
                candidate.family_kind = family.kind
            if family.pattern not in first_for_pattern:
                candidate.collapsed_count = max(
                    candidate.collapsed_count, family.discovered_count
                )
                candidate.grouped_labels = family.sample_labels
                first_for_pattern.add(family.pattern)

        return [self._family_payload(family) for family in families]

    def _is_global_capability(self, capability: TransitionCapability) -> bool:
        if capability.global_key is None:
            return False
        sources = {
            source_id
            for source_id, _ in self._global_capabilities[capability.global_key]
        }
        return len(sources) >= 2

    def _capability_for(
        self, source: StateMeta, item: Interactable
    ) -> TransitionCapability | None:
        capability_id = self._capability_by_item.get((source.id, item.item_id))
        if capability_id is None:
            return None
        return self._capabilities.get((source.id, capability_id))

    async def _resolve_capability(self, ref: tuple[str, str]) -> None:
        capability = self._capabilities.get(ref)
        if capability is None:
            return
        source = self._states.get(capability.source_id)
        if source is None:
            return
        if capability.global_key is not None:
            return

        target_id: str | None = None
        if capability.target_hint is not None:
            target_id = self._known_target_id(source.auth_context, capability.target_hint)
        if target_id is None and capability.item.kind == "tab":
            page_id = self._page_ancestor(source)
            target_id = self._tab_targets.get((page_id, capability.control_key))

        if target_id == source.id:
            return
        if target_id is None or target_id not in self._states:
            if capability.target_hint is not None:
                self._waiting_by_target[
                    (source.auth_context, capability.target_hint)
                ].add(ref)
            return
        if (
            capability.global_key is not None
            and not self._should_materialize_inferred_edge(source, capability.item)
        ):
            return

        await self._add_edge(
            source,
            self._states[target_id],
            ActionCandidate(interactable=capability.item, score=100.0),
            via="inferred",
            capability=capability,
        )

    async def _register_transition_capabilities(
        self, meta: StateMeta
    ) -> None:
        """Persist visible transition evidence independently from exploration.

        Ranking and substate delta filtering decide what to click; they must not
        decide which visibly supported transitions exist in the state machine.
        """
        items = meta.interactables
        if meta.state_type == StateType.MODAL:
            modal_items = [item for item in items if item.in_modal]
            if modal_items:
                items = modal_items

        touched_globals: set[str] = set()
        touched_tabs: set[tuple[str, str]] = set()
        for item in items:
            if not evaluate_action(item, base_url=self._root_url).allowed:
                continue
            control_key = self._control_key(item)
            item.control_key = control_key
            capability_id = self._capability_id(meta, item)
            target_hint = self._target_hint(item)
            global_key = self._global_key(meta, item, target_hint)
            capability = TransitionCapability(
                source_id=meta.id,
                capability_id=capability_id,
                control_key=control_key,
                item=item,
                target_hint=target_hint,
                global_key=global_key,
            )
            ref = (meta.id, capability_id)
            self._capabilities[ref] = capability
            self._capability_by_item[(meta.id, item.item_id)] = capability_id

            if global_key is not None:
                self._global_capabilities[global_key].add(ref)
                touched_globals.add(global_key)

            if item.kind == "tab":
                tab_key = (self._page_ancestor(meta), control_key)
                self._tab_capabilities[tab_key].add(ref)
                touched_tabs.add(tab_key)
                if item.aria_selected is True:
                    self._tab_targets.setdefault(tab_key, meta.id)

            await self._resolve_capability(ref)

        # A newly observed selected tab or newly repeated navbar signature can
        # resolve controls captured in earlier states.
        for tab_key in touched_tabs:
            if tab_key in self._tab_targets:
                for ref in tuple(self._tab_capabilities[tab_key]):
                    await self._resolve_capability(ref)
        for global_key in touched_globals:
            refs = tuple(self._global_capabilities[global_key])
            if len({source_id for source_id, _ in refs}) >= 2:
                for ref in refs:
                    await self._resolve_capability(ref)

        target_key = (meta.auth_context, meta.url_normalized)
        waiting = tuple(self._waiting_by_target.pop(target_key, set()))
        for ref in waiting:
            await self._resolve_capability(ref)

    async def _learn_performed_capability(
        self, source: StateMeta, item: Interactable, destination: StateMeta
    ) -> None:
        capability = self._capability_for(source, item)
        if capability is None:
            return

        if capability.item.kind == "tab":
            tab_key = (self._page_ancestor(source), capability.control_key)
            self._tab_targets.setdefault(tab_key, destination.id)
            for ref in tuple(self._tab_capabilities[tab_key]):
                await self._resolve_capability(ref)

        global_key = capability.global_key
        if global_key is None or not self._is_global_capability(capability):
            return
        known = self._global_targets.get(global_key)
        if known is not None and known != destination.id:
            self._ambiguous_global_targets.add(global_key)
            self._global_targets.pop(global_key, None)
            return
        if global_key in self._ambiguous_global_targets:
            return
        self._global_targets[global_key] = destination.id

    async def _execute(self, page: Page, pending: PendingAction) -> None:
        source = pending.from_state
        item = pending.candidate.interactable
        edge_key = (source.id, item.selector)
        if edge_key in self._edges_done:
            return

        # Cost-free shortcut: a link to a page we've already mapped becomes an
        # inferred edge -- no click, no action budget spent.
        self_page_id = self._self_link_target(item, source)
        if self_page_id is not None:
            page_id = self._url_to_state.get((source.auth_context, self_page_id))
            if page_id is None:
                page_id = self._url_to_state.get(self_page_id)
            if page_id is None:
                page_id = source.id
            destination = self._states[page_id]
            self._edges_done.add(edge_key)
            self._item_outcome[(source.id, item.item_id)] = "explored"
            if destination.id == source.id:
                self._stats["noop_actions"] += 1
                self._emit_action_finished(
                    ActionOutcome.NOOP,
                    f"'{item.label}' is a self-link (no navigation)",
                    from_state_id=source.id,
                )
            else:
                self._stats["dedup_hits"] += 1
                await self._add_edge(
                    source, destination, pending.candidate, via="inferred"
                )
                self._emit_action_finished(
                    ActionOutcome.DEDUPED,
                    f"'{item.label}' links to known s{destination.index}",
                    from_state_id=source.id,
                    to_state_id=destination.id,
                )
            return

        inferred_id = self._inferred_target(source, item)
        if inferred_id is not None:
            await self._add_edge(
                source, self._states[inferred_id], pending.candidate, via="inferred"
            )
            self._edges_done.add(edge_key)
            self._item_outcome[(source.id, item.item_id)] = "explored"
            self._emit_action_finished(
                ActionOutcome.DEDUPED,
                f"Inferred '{item.label}' -> known s{self._states[inferred_id].index}",
                from_state_id=source.id,
                to_state_id=inferred_id,
            )
            return

        family_pattern = pending.candidate.family_pattern
        if family_pattern is not None:
            self._family_info.setdefault(
                family_pattern,
                {
                    "id": pending.candidate.family_id,
                    "label": pending.candidate.family_label,
                    "kind": pending.candidate.family_kind,
                    "pattern": family_pattern,
                    "label_source": "heuristic",
                    "confidence": 0.85,
                    "discovered_count": max(1, pending.candidate.collapsed_count),
                    "sample_labels": list(pending.candidate.grouped_labels),
                },
            )
            info = self._family_info[family_pattern]
            info["discovered_count"] = max(
                int(info.get("discovered_count", 1)), pending.candidate.collapsed_count
            )
            attempts = self._family_attempts.get(family_pattern, 0)
            if attempts >= self._config.exploration.url_family_cap:
                self._family_skipped[family_pattern] = (
                    self._family_skipped.get(family_pattern, 0) + 1
                )
                self._stats["family_urls_skipped"] += 1
                self._edges_done.add(edge_key)
                self._item_outcome[(source.id, item.item_id)] = "skipped_duplicate"
                self._emit_action_finished(
                    ActionOutcome.DEDUPED,
                    f"Skipped '{item.label}' after sampling route family {family_pattern}",
                    from_state_id=source.id,
                    route_family=family_pattern,
                )
                return
            self._family_attempts[family_pattern] = attempts + 1

        self._budget.note_action()
        self._actions_since_new += 1
        self._emit(
            ExplorerEvent(
                EventType.ACTION_STARTED,
                f"Trying '{item.label}' from s{source.index}",
                {
                    "from_state_id": source.id,
                    "from_index": source.index,
                    "label": item.label,
                    "selector": item.selector,
                    "score": round(pending.candidate.score, 1),
                },
            )
        )

        if not await self._ensure_at(page, source):
            self._stats["failed_actions"] += 1
            self._emit_action_finished(
                ActionOutcome.FAILED,
                f"Could not replay path to s{source.index}",
                from_state_id=source.id,
            )
            return

        if not await validate_interactable(page, item):
            self._stats["failed_actions"] += 1
            self._item_outcome[(source.id, item.item_id)] = "noop"
            self._edges_done.add(edge_key)
            self._emit_action_finished(
                ActionOutcome.FAILED,
                f"Dropped stale action {item.label!r}",
                from_state_id=source.id,
            )
            return

        try:
            await click_interactable(
                page,
                item,
                timeout_ms=self._config.exploration.action_timeout_ms,
                base_url=self._root_url,
            )
        except PlaywrightError:
            self._current_state_id = None  # page state is now unknown
            self._stats["failed_actions"] += 1
            self._item_outcome[(source.id, item.item_id)] = "explored"
            self._edges_done.add(edge_key)  # don't burn budget retrying a dead selector
            self._emit_action_finished(
                ActionOutcome.FAILED,
                f"Click failed on {item.label!r}",
                from_state_id=source.id,
            )
            return

        await self._absorb_popups(page)
        observation = await observe_page(
            page, self._config, auth_context=self._auth_context
        )
        if (
            observation.url_normalized == source.url_normalized
            and self._is_generic_expansion(item)
            and not observation.snapshot.signals.modal_open
        ):
            self._stats["noop_actions"] += 1
            self._current_state_id = source.id
            self._item_outcome[(source.id, item.item_id)] = "noop"
            self._edges_done.add(edge_key)
            self._emit_action_finished(
                ActionOutcome.NOOP,
                f"'{item.label}' is a generic expansion, not a durable state",
                from_state_id=source.id,
            )
            return
        if family_pattern is not None:
            self._family_sampled_urls.setdefault(family_pattern, []).append(
                observation.snapshot.url
            )
        key = identity.key_for(observation)
        existing_id = self._resolve_existing_state(observation, source, key)
        if existing_id is None and family_pattern is not None:
            existing_id = self._family_template_target(observation, family_pattern)

        if existing_id == source.id:
            # The action changed nothing meaningful; don't record a self-loop.
            self._stats["noop_actions"] += 1
            self._current_state_id = source.id
            self._item_outcome[(source.id, item.item_id)] = "noop"
            self._emit_action_finished(
                ActionOutcome.NOOP,
                f"'{item.label}' changed nothing",
                from_state_id=source.id,
            )
            return

        via = "performed"
        if existing_id is not None:
            destination = self._states[existing_id]
            self._stats["dedup_hits"] += 1
            if family_pattern is not None and destination.route_family == family_pattern:
                self._stats["family_dedup_hits"] += 1
            self._emit_action_finished(
                ActionOutcome.DEDUPED,
                f"'{item.label}' led to known state s{destination.index}",
                from_state_id=source.id,
                to_state_id=destination.id,
            )
        else:
            destination = await self._register_state(
                observation,
                depth=source.depth + 1,
                path=self._path_for(observation, source, item.selector, item.label),
                key=key,
                source=source,
                trigger=item,
                route_family=family_pattern,
                journey_key=pending.journey_key,
                family=self._family_info.get(family_pattern),
                enqueue_actions=pending.phase != 0,
            )
            self._actions_since_new = 0
            self._emit_action_finished(
                ActionOutcome.NEW_STATE,
                f"'{item.label}' reached new state s{destination.index}",
                from_state_id=source.id,
                to_state_id=destination.id,
            )

        await self._add_edge(source, destination, pending.candidate, via=via)
        await self._learn_performed_capability(source, item, destination)
        self._edges_done.add(edge_key)
        self._item_outcome[(source.id, item.item_id)] = "explored"
        # A family merge leaves the browser on an alternate instance URL.
        # Force replay before expanding the representative's actions.
        self._current_state_id = (
            destination.id
            if observation.url_normalized == destination.url_normalized
            else None
        )
        if self._current_state_id == destination.id:
            await self._validate_browser_back(page, source, destination)

        # Auth-wall gate: pause exploration if the destination is a login page.
        # This runs after recording the edge so the gate node is always visible.
        if destination.state_type == StateType.AUTH_WALL:
            if self._auth_mode == AuthMode.LOGIN:
                post_auth = await self._handle_auth_wall(page, destination)
                if post_auth is not None:
                    await self._add_user_auth_edge(destination, post_auth)
                    self._current_state_id = post_auth.id
        elif pending.phase == 0:
            self._enqueue_auth_discovery(
                destination, analyze_state(observation, base_url=self._root_url)
            )

    def _resolve_existing_state(
        self, observation: Observation, source: StateMeta, key: identity.StateKey
    ) -> str | None:
        """Layered match: identity index, then known top-level URL page.

        URL fallback applies only to cross-URL navigations so same-URL SPA
        sub-states (tabs, modals, dropdowns) still rely on DOM identity.
        """
        existing_id = self._identity.find(key)
        if existing_id is not None:
            return existing_id
        if (
            observation.snapshot.signals.password_fields > 0
            or observation.snapshot.signals.username_fields > 0
        ):
            auth_url = _auth_surface_url(observation.snapshot.url)
            if auth_url is not None:
                known_auth = getattr(self, "_auth_surface_to_state", {}).get(
                    (observation.auth_context, auth_url)
                )
                if known_auth is not None:
                    return known_auth
        if observation.url_normalized == source.url_normalized:
            return None
        if observation.snapshot.signals.modal_open:
            return None
        existing = self._url_to_state.get(
            (observation.auth_context, observation.url_normalized)
        )
        if existing is None:
            existing = self._url_to_state.get(observation.url_normalized)
        return existing

    @staticmethod
    def _self_link_target(item: Interactable, source: StateMeta) -> str | None:
        """Normalized href pointing at the current page (e.g. logo/home link)."""
        href = item.href
        if not href or href.startswith("javascript:"):
            return None
        if not href.startswith(("http://", "https://", "file://")):
            return None
        if normalize_url(href) != source.url_normalized:
            return None
        return source.url_normalized

    @staticmethod
    def _family_template_key(observation: Observation) -> tuple:
        """Conservative cross-URL template identity for a content family."""
        signals = observation.snapshot.signals
        return (
            signals.modal_open,
            signals.form_count,
            signals.password_fields,
            signals.username_fields,
            signals.payment_fields,
            observation.skeleton_hash,
            observation.action_sig,
        )

    def _family_template_target(
        self, observation: Observation, family_pattern: str
    ) -> str | None:
        """Match only exact structure/affordances within an inferred family."""
        return self._family_variants.get(family_pattern, {}).get(
            self._family_template_key(observation)
        )

    # ------------------------------------------------------------------
    # State registration
    # ------------------------------------------------------------------

    def _page_ancestor(self, state: StateMeta) -> str:
        """Climb the parent chain to the URL page a sub-state belongs to."""
        current = state
        while current.parent_state_id is not None:
            parent = self._states.get(current.parent_state_id)
            if parent is None:
                break
            current = parent
        return current.id

    @staticmethod
    def _substate_type(trigger: Interactable | None) -> StateType:
        """Subtype for a same-URL state change that isn't a modal."""
        if trigger is not None:
            if trigger.role == "tab" or trigger.kind == "tab":
                return StateType.TAB
            label = trigger.label.lower()
            if (
                trigger.tag == "summary"
                or trigger.kind in ("disclosure", "menuitem")
                or trigger.aria_expanded is not None
                or re.search(r"\b(menu|settings?|account|profile|filter|sort)\b", label)
            ):
                return StateType.DROPDOWN
        return StateType.DROPDOWN

    async def _register_state(
        self,
        observation: Observation,
        *,
        depth: int,
        path: list[ActionStep],
        key: identity.StateKey | None = None,
        source: StateMeta | None = None,
        trigger: Interactable | None = None,
        route_family: str | None = None,
        journey_key: str | None = None,
        enqueue_actions: bool = True,
        family: dict | None = None,
        page_depth_override: int | None = None,
    ) -> StateMeta:
        analysis = analyze_state(observation, base_url=self._root_url)
        surface_families = self._apply_surface_families(observation, analysis)
        nav_capabilities = self._nav_capabilities_for(
            observation.auth_context, observation.interactables
        )
        state_type = analysis.state_type
        if not is_same_origin(observation.snapshot.url, self._root_url):
            state_type = StateType.EXTERNAL  # reached via script redirect; never expanded

        # Same-URL sub-states (modal/tab/dropdown) hang off the page that
        # opened them; refine the type for non-modal structural changes.
        parent_state_id: str | None = None
        return_state_id: str | None = None
        page_depth = 0
        substate_depth = 0
        if source is not None and state_type == StateType.MODAL:
            # Modals can reflect their open state in the query string.  They
            # still belong to the page that opened them and should return to
            # the exact source state, not merely the same normalized URL.
            parent_state_id = self._page_ancestor(source)
            return_state_id = source.id
            page_depth = source.page_depth
            substate_depth = source.substate_depth + 1
        elif source is not None and observation.url_normalized == source.url_normalized:
            parent_state_id = self._page_ancestor(source)
            return_state_id = source.id
            page_depth = source.page_depth
            substate_depth = source.substate_depth + 1
            if state_type == StateType.PAGE:
                state_type = self._substate_type(trigger)
        elif source is not None:
            page_depth = source.page_depth + 1
        if page_depth_override is not None:
            page_depth = page_depth_override

        if route_family is None and parent_state_id is None:
            url_family = infer_url_family(observation.snapshot.url)
            if url_family is not None:
                route_family = url_family.pattern
                existing_family = self._family_info.get(route_family, {})
                url_payload = self._url_family_payload(url_family)
                sample_label = observation.snapshot.title or observation.snapshot.url
                family = {
                    **url_payload,
                    **existing_family,
                    "discovered_count": max(
                        int(existing_family.get("discovered_count", 0)),
                        int(url_payload.get("discovered_count", 1)),
                    ),
                    "sample_labels": list(
                        dict.fromkeys(
                            [
                                *existing_family.get("sample_labels", []),
                                sample_label,
                            ]
                        )
                    )[:8],
                    "sample_urls": list(
                        dict.fromkeys(
                            [
                                *existing_family.get("sample_urls", []),
                                observation.snapshot.url,
                            ]
                        )
                    )[:8],
                }
                self._family_info[route_family] = dict(family)

        page_role = infer_page_role(
            observation,
            analysis,
            state_type,
            depth=page_depth,
            route_family=route_family,
        )
        naming = heuristic_name(
            observation,
            state_type=state_type,
            trigger_label=trigger.label if trigger else None,
            parent_label=source.display_label if source else None,
        )

        state = persist_state(
            observation,
            run_id=self._run_id,
            state_id=new_id(),
            depth=depth,
            path=path,
            store=self._store,
            save_dom=self._config.capture.save_dom_snapshots,
        )
        state.state_type = state_type
        state.detected_flags = {
            **analysis.flags,
            "auth_context": observation.auth_context.value,
            "page_role": page_role.value,
            "name": naming,
        }

        async with self._sessions() as session:
            row = build_state_row(state)
            row.parent_state_id = parent_state_id
            row.label = naming["text"]
            row.exploration = {
                "auth_context": observation.auth_context.value,
                "page_role": page_role.value,
                "page_depth": page_depth,
                "substate_depth": substate_depth,
                "nav_capabilities": nav_capabilities,
                "surface_families": surface_families,
                **({"return_state_id": return_state_id} if return_state_id else {}),
                "name": naming,
                **({"route_family": route_family} if route_family else {}),
                **({"family": dict(family)} if family else {}),
            }
            session.add(row)
            await session.commit()

        representative_ids = {c.interactable.item_id for c in analysis.candidates}
        blocked_ids = {c.interactable.item_id for c, _ in analysis.denied}
        meta = StateMeta(
            id=state.state_id,
            index=len(self._states),
            url=observation.snapshot.url,
            url_normalized=observation.url_normalized,
            depth=depth,
            path=path,
            state_type=state_type,
            parent_state_id=parent_state_id,
            interactables=observation.interactables,
            representative_ids=representative_ids,
            blocked_ids=blocked_ids,
            route_family=route_family,
            auth_context=observation.auth_context,
            page_depth=page_depth,
            substate_depth=substate_depth,
            journey_key=journey_key or (source.journey_key if source else "root"),
            family=dict(family) if family else None,
            page_role=page_role,
            display_label=naming["text"],
            return_state_id=return_state_id,
            nav_capabilities=nav_capabilities,
            surface_families=surface_families,
        )
        self._states[meta.id] = meta
        self._identity.add(key or identity.key_for(observation), meta.id)
        self._visited_urls.add(observation.url_normalized)
        self._url_to_state.setdefault(
            (observation.auth_context, observation.url_normalized), meta.id
        )
        if state_type == StateType.AUTH_WALL:
            auth_url = _auth_surface_url(observation.snapshot.url)
            if auth_url is not None:
                self._auth_surface_to_state.setdefault(
                    (observation.auth_context, auth_url), meta.id
                )
        if parent_state_id is None and route_family is not None:
            self._family_variants.setdefault(route_family, {}).setdefault(
                self._family_template_key(observation), meta.id
            )
        self._stats["states"] += 1
        self._stats["actions_denied"] += len(analysis.denied)

        title = naming["text"]
        message = f"s{meta.index} [{state_type.value}] {title!r} (depth {depth})"
        if analysis.denied:
            message += f" - {len(analysis.denied)} risky action(s) blocked"
        self._emit(
            ExplorerEvent(
                EventType.STATE_DISCOVERED,
                message,
                {
                    "state_id": meta.id,
                    "index": meta.index,
                    "url": observation.snapshot.url,
                    "url_normalized": observation.url_normalized,
                    "title": title,
                    "type": state_type.value,
                    "depth": depth,
                    "parent_state_id": parent_state_id,
                    "screenshot": state.screenshot_path,
                    "flags": state.detected_flags,
                    "label": naming["text"],
                    "page_role": page_role.value,
                    "name": naming,
                    "route_family": route_family,
                    "auth_context": observation.auth_context.value,
                    "page_depth": page_depth,
                    "substate_depth": substate_depth,
                    "return_state_id": return_state_id,
                    "family": family,
                    "nav_capabilities": nav_capabilities,
                    "surface_families": surface_families,
                    "denied_count": len(analysis.denied),
                    "surface_items": self._surface_summary(meta),
                    "counters": self._counters(),
                },
            )
        )

        await self._register_transition_capabilities(meta)
        if enqueue_actions:
            self._enqueue_actions(meta, analysis)
        return meta

    @staticmethod
    def _surface_summary(meta: StateMeta) -> list[dict]:
        """Compact surface-item list for the live state_discovered payload."""
        return [
            {
                "item_id": item.item_id,
                "label": item.label,
                "kind": item.kind,
                "region": item.region,
                "fold": item.fold,
                "group_id": item.group_id,
                "aria_selected": item.aria_selected,
                "aria_expanded": item.aria_expanded,
                "control_key": item.control_key,
                "container_key": item.container_key,
                "status": (
                    "blocked" if item.item_id in meta.blocked_ids else "pending"
                ),
            }
            for item in meta.interactables
        ]

    def _path_for(
        self,
        observation: Observation,
        source: StateMeta,
        selector: str,
        label: str | None,
    ) -> list[ActionStep]:
        """Replay path for a new state: URL-addressable states restart from a
        goto; sub-URL states (modals, tabs) extend the source state's path."""
        if (
            observation.url_normalized != source.url_normalized
            and not observation.snapshot.signals.modal_open
        ):
            return [ActionStep(kind="goto", url=observation.snapshot.url)]
        return [*source.path, ActionStep(kind="click", selector=selector, label=label)]

    def _enqueue_actions(self, meta: StateMeta, analysis: StateAnalysis) -> None:
        if meta.page_depth >= self._config.budgets.max_depth:
            meta.representative_ids = set()
            return
        if meta.substate_depth >= self._config.exploration.max_substate_depth:
            meta.representative_ids = set()
            return
        if meta.state_type in _TERMINAL_TYPES:
            meta.representative_ids = set()
            return
        if meta.state_type in _GATE_TYPES:
            meta.representative_ids = set()
            return  # expanded only after auth_gate resolution via _handle_auth_wall
        eligible: list[ActionCandidate] = []
        parent_selector_shapes: set[str] = set()
        if meta.parent_state_id:
            parent = self._states.get(meta.parent_state_id)
            if parent is not None:
                parent_selector_shapes = {
                    identity.strip_positional_selector(item.selector)
                    for item in parent.interactables
                }
        reserved_by_family: dict[str, int] = {}
        seen_semantic_actions: set[tuple[str, str | None]] = set()
        for candidate in analysis.safe:
            item = candidate.interactable
            target_hint = self._target_hint(item)
            known_target = (
                self._known_target_id(meta.auth_context, target_hint)
                if target_hint is not None
                else None
            )
            if target_hint == meta.url_normalized:
                self._item_outcome[(meta.id, item.item_id)] = "noop"
                continue
            if (
                self._is_peripheral_action(item)
                and not is_auth_entry(candidate)
            ):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                self._is_global_chrome_item(item)
                and meta.depth > 0
                and meta.page_role != PageRole.HOME
                and not is_auth_entry(candidate)
            ):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                meta.auth_context == AuthContext.GUEST
                and re.search(r"\bsettings?\b", item.label, re.I)
            ):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                self._is_generic_expansion(item)
                and self._is_global_chrome_item(item)
                and not meta.return_state_id
            ):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                known_target is not None
                and self._is_global_chrome_item(item)
                and not self._should_materialize_inferred_edge(meta, item)
            ):
                # Repeated navbar/sidebar links are capability evidence. Do not
                # spend frontier budget or create another inferred primary edge.
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                item.label.lower().startswith("unlabelled ")
                and item.region not in {"nav", "header", "modal"}
            ):
                continue
            semantic_key = (item.label.strip().lower(), item.region)
            if semantic_key in seen_semantic_actions:
                continue
            seen_semantic_actions.add(semantic_key)
            if (
                parent_selector_shapes
                and identity.strip_positional_selector(item.selector)
                in parent_selector_shapes
                and not (
                    (item.kind == "tab" and meta.state_type == StateType.TAB)
                    or item.in_modal
                    or item.region == "modal"
                )
            ):
                # Unchanged navigation/footer chrome belongs to the parent.
                continue
            family = candidate.family_pattern
            if family is not None:
                reserved = reserved_by_family.get(family, 0)
                attempted = self._family_attempts.get(family, 0)
                if attempted + reserved >= self._config.exploration.url_family_cap:
                    self._family_skipped[family] = self._family_skipped.get(family, 0) + 1
                    self._stats["family_urls_skipped"] += 1
                    self._item_outcome[(meta.id, candidate.interactable.item_id)] = (
                        "skipped_duplicate"
                    )
                    continue
                reserved_by_family[family] = reserved + 1
            candidate.score = score_action(candidate, visited_urls=self._visited_urls)
            eligible.append(candidate)
        ranked = sorted(eligible, key=lambda c: c.score, reverse=True)
        top = ranked[: self._config.exploration.max_actions_per_state]
        # Always reserve a slot for a sign-in/sign-up affordance when one exists.
        auth_pick = next((c for c in ranked if is_auth_entry(c)), None)
        if auth_pick and auth_pick not in top:
            top = [auth_pick, *[c for c in top if c is not auth_pick]][
                : self._config.exploration.max_actions_per_state
            ]
        # A collection surface should sample one repeated entity family even
        # when filters and navigation links fill the top-K slots.
        family_pick = next((c for c in ranked if c.family_pattern), None)
        if family_pick and family_pick not in top and top:
            top = [*top[:-1], family_pick]
        if meta.return_state_id is not None:
            return_pick = next(
                (
                    candidate
                    for candidate in ranked
                    if re.search(
                        r"\b(close|cancel|dismiss|back|return|previous)\b",
                        candidate.interactable.label,
                        re.I,
                    )
                    or (
                        meta.state_type == StateType.DROPDOWN
                        and candidate.interactable.kind == "disclosure"
                    )
                ),
                None,
            )
            if return_pick and return_pick not in top:
                top = [return_pick, *top][
                    : self._config.exploration.max_actions_per_state
                ]
        queued_ids = {c.interactable.item_id for c in top}
        meta.representative_ids = queued_ids
        for candidate in eligible:
            item_id = candidate.interactable.item_id
            if item_id not in queued_ids:
                self._item_outcome.setdefault((meta.id, item_id), "skipped_duplicate")
        for candidate in top:
            journey_key = meta.journey_key
            if meta.depth == 0:
                journey_key = _journey_slug(candidate.interactable.label)
            self._frontier.push(
                PendingAction(
                    from_state=meta,
                    candidate=candidate,
                    journey_key=journey_key,
                )
            )

    def _enqueue_auth_discovery(
        self, meta: StateMeta, analysis: StateAnalysis
    ) -> None:
        """Seed login mode with auth actions before any general journey."""
        ranked: list[ActionCandidate] = []
        revealers: list[ActionCandidate] = []
        for candidate in analysis.safe:
            candidate.score = score_action(candidate, visited_urls=self._visited_urls)
            if is_auth_entry(candidate):
                ranked.append(candidate)
            else:
                item = candidate.interactable
                label = item.label.lower()
                if (
                    item.region in {"nav", "header"}
                    and item.kind in {"button", "menuitem", "disclosure"}
                    and any(word in label for word in ("menu", "account", "profile"))
                ):
                    revealers.append(candidate)
        picks = sorted(ranked, key=lambda c: c.score, reverse=True)
        if not picks:
            picks = sorted(revealers, key=lambda c: c.score, reverse=True)[
                : self._config.exploration.auth_discovery_action_cap
            ]
        meta.representative_ids = {c.interactable.item_id for c in picks}
        for candidate in picks:
            self._frontier.push(
                PendingAction(
                    from_state=meta,
                    candidate=candidate,
                    journey_key="authentication",
                    phase=0,
                )
            )

        # If no authentication entry can be found, pause at the root instead
        # of silently pretending this is an authenticated run.
        if not picks:
            self._enqueue_actions(meta, analysis)

    # ------------------------------------------------------------------
    # Auth gate
    # ------------------------------------------------------------------

    async def _handle_auth_wall(
        self, page: Page, meta: StateMeta
    ) -> StateMeta | None:
        """Authenticate, retrying the checkpoint until success or explicit skip."""
        autofill_attempted = False
        while True:
            observation: Observation | None = None
            autofill_submitted = False
            if self._credentials is not None:
                autofill_attempted = True
                try:
                    auth_page_url = page.url
                    submitted = await autofill_auth_form(
                        page,
                        self._credentials,
                        timeout_ms=self._config.exploration.action_timeout_ms,
                    )
                    autofill_submitted = submitted
                    if submitted:
                        with contextlib.suppress(PlaywrightError):
                            await page.wait_for_function(
                                """
                                (sourceUrl) => {
                                  if (location.href !== sourceUrl) return true;
                                  const fields = [...document.querySelectorAll(
                                    'input[type="password"]'
                                  )];
                                  return !fields.some((field) => {
                                    const style = getComputedStyle(field);
                                    const rect = field.getBoundingClientRect();
                                    return style.display !== 'none'
                                      && style.visibility !== 'hidden'
                                      && rect.width > 0 && rect.height > 0;
                                  });
                                }
                                """,
                                arg=auth_page_url,
                                timeout=self._config.browser.navigation_timeout_ms,
                            )
                        await stabilize(page, self._config.browser.stabilize_quiet_ms)
                    observation = await observe_page(
                        page, self._config, auth_context=AuthContext.GUEST
                    )
                except Exception:  # noqa: BLE001 - user can recover at the gate
                    observation = None

            if observation is not None and self._auth_succeeded(observation, meta):
                return await self._register_authenticated_state(observation, meta)

            self._emit(
                ExplorerEvent(
                    EventType.AUTH_GATE,
                    f"Authentication required at {meta.url!r}",
                    {
                        "state_id": meta.id,
                        "url": meta.url,
                        "title": meta.url,
                        "screenshot": meta.id,
                        "decision": None,
                        "autofill_attempted": autofill_attempted,
                        "autofill_submitted": autofill_submitted,
                        "observed_url": (
                            observation.snapshot.url if observation is not None else page.url
                        ),
                        "suggested_actions": ["resume", "skip"],
                    },
                )
            )
            if self._auth_gate_hook is None:
                return None

            decision, new_credentials = await self._auth_gate_hook(meta.id, meta.url)
            if new_credentials is not None:
                self._credentials = new_credentials
            if decision == "skip":
                self._auth_mode = AuthMode.GUEST
                self._config.authentication.mode = AuthMode.GUEST
                await self._mark_auth_skipped(meta.id)
                return None

            # A headed run may have been authenticated manually while paused.
            try:
                observation = await observe_page(
                    page, self._config, auth_context=AuthContext.GUEST
                )
            except Exception:  # noqa: BLE001
                observation = None
            if observation is not None and self._auth_succeeded(observation, meta):
                return await self._register_authenticated_state(observation, meta)
            autofill_attempted = False

    @staticmethod
    def _auth_succeeded(observation: Observation, source: StateMeta) -> bool:
        if observation.snapshot.signals.password_fields > 0:
            return False
        analysis = analyze_state(observation, base_url=source.url)
        if analysis.state_type == StateType.AUTH_WALL:
            return False
        changed_page = observation.url_normalized != source.url_normalized
        return bool(_AUTH_SUCCESS.search(observation.snapshot.visible_text)) or changed_page

    async def _register_authenticated_state(
        self, observation: Observation, source: StateMeta
    ) -> StateMeta:
        self._auth_context = AuthContext.AUTHENTICATED
        observation = with_auth_context(observation, AuthContext.AUTHENTICATED)
        # All guest candidates are stale once the site's header/session changes.
        self._frontier = Frontier()
        post_auth = await self._register_state(
            observation,
            depth=source.depth + 1,
            path=[ActionStep(kind="goto", url=observation.snapshot.url)],
            key=identity.key_for(observation),
            source=source,
            journey_key="authentication",
            page_depth_override=0,
        )
        self._actions_since_new = 0
        self._emit(
            ExplorerEvent(
                EventType.AUTH_GATE,
                f"Authentication succeeded; reached s{post_auth.index}",
                {
                    "state_id": source.id,
                    "url": source.url,
                    "title": observation.snapshot.title,
                    "screenshot": source.id,
                    "decision": "autofilled",
                    "post_auth_state_id": post_auth.id,
                    "autofill_attempted": True,
                },
            )
        )
        return post_auth

    async def _mark_auth_skipped(self, state_id: str) -> None:
        with contextlib.suppress(Exception):
            async with self._sessions() as session:
                row = await session.get(db.StateNode, state_id)
                if row is not None:
                    flags = dict(row.detected_flags or {})
                    flags["auth_gate_skipped"] = True
                    row.detected_flags = flags
                    await session.commit()

    async def _add_user_auth_edge(
        self, source: StateMeta, destination: StateMeta
    ) -> None:
        """Record a user-authenticated transition (manual or credential autofill)."""
        await self._upsert_transition(
            source,
            destination,
            capability_id="user-auth",
            action_type="user_auth",
            label="User authenticated",
            selector="auth",
            element_text=None,
            confidence=1.0,
            collapsed_count=1,
            via="user_auth",
            surface_item_id=None,
            transition_kind="auth",
            scope="local",
            reversible=False,
            evidence={"mode": "user_auth", "validated": True},
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def _ensure_at(self, page: Page, target: StateMeta) -> bool:
        """Make sure the page is at `target`, replaying its path if needed."""
        if self._current_state_id == target.id:
            return True
        try:
            for step in target.path:
                if step.kind == "goto":
                    await page.goto(step.url)
                else:
                    await click_selector(
                        page,
                        step.selector,
                        timeout_ms=self._config.exploration.action_timeout_ms,
                    )
                await stabilize(page, self._config.browser.stabilize_quiet_ms)
        except PlaywrightError:
            self._current_state_id = None
            return False
        self._current_state_id = target.id
        return True

    async def _validate_browser_back(
        self, page: Page, source: StateMeta, destination: StateMeta
    ) -> None:
        """Record contextual browser Back only after exact restoration.

        Back is history-dependent, so this check is performed once for each
        actually executed source/destination context and never inferred as a
        blanket reverse edge.
        """
        pair = (source.id, destination.id)
        if pair in self._validated_history:
            return
        self._validated_history.add(pair)
        if source.url_normalized == destination.url_normalized:
            return
        if destination.state_type in {StateType.AUTH_WALL, StateType.EXTERNAL}:
            return
        if not (
            is_same_origin(source.url, self._root_url)
            and is_same_origin(destination.url, self._root_url)
        ):
            return

        self._stats["restoration_checks"] += 1
        restored_id: str | None = None
        went_back = False
        try:
            await page.go_back(
                wait_until="domcontentloaded",
                timeout=self._config.exploration.action_timeout_ms,
            )
            went_back = True
            await stabilize(page, self._config.browser.stabilize_quiet_ms)
            restored = await observe_page(
                page, self._config, auth_context=self._auth_context
            )
            restored_id = self._resolve_existing_state(
                restored, destination, identity.key_for(restored)
            )
            if restored_id == source.id:
                await self._upsert_transition(
                    destination,
                    source,
                    capability_id=f"history:{source.id}",
                    action_type="browser_back",
                    label=f"Back to {source.display_label or f's{source.index}'}",
                    selector=f"history:{source.id}",
                    element_text=None,
                    confidence=1.0,
                    collapsed_count=1,
                    via="performed",
                    surface_item_id=None,
                    transition_kind="back",
                    scope="local",
                    reversible=True,
                    evidence={
                        "mode": "performed",
                        "mechanism": "browser_history",
                        "expected_state_id": source.id,
                        "restored_state_id": restored_id,
                        "validated": True,
                    },
                )
        except PlaywrightError:
            restored_id = None
        finally:
            if not went_back:
                self._current_state_id = destination.id
            else:
                try:
                    await page.go_forward(
                        wait_until="domcontentloaded",
                        timeout=self._config.exploration.action_timeout_ms,
                    )
                    await stabilize(page, self._config.browser.stabilize_quiet_ms)
                    forward = await observe_page(
                        page, self._config, auth_context=self._auth_context
                    )
                    forward_id = self._resolve_existing_state(
                        forward, source, identity.key_for(forward)
                    )
                    self._current_state_id = (
                        destination.id if forward_id == destination.id else None
                    )
                except PlaywrightError:
                    self._current_state_id = None
                if self._current_state_id != destination.id:
                    await self._ensure_at(page, destination)

    async def _absorb_popups(self, page: Page) -> None:
        """Close any popup/new-tab pages; follow same-origin targets in the
        main page so the action's effect is still observed."""
        for extra in [p for p in page.context.pages if p is not page]:
            popup_url = extra.url
            with contextlib.suppress(PlaywrightError):
                await extra.close()
            if popup_url and is_same_origin(popup_url, self._root_url):
                try:
                    await page.goto(popup_url)
                except PlaywrightError:
                    self._current_state_id = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _transition_kind(
        source: StateMeta, destination: StateMeta, item: Interactable
    ) -> str:
        label = item.label.strip().lower()
        if item.kind == "tab" or item.role == "tab":
            return "tab"
        if re.search(r"\b(cancel|dismiss)\b", label):
            return "cancel"
        if re.search(r"\b(close|back|return|previous)\b", label):
            if "back" in label:
                return "back"
            if "previous" in label:
                return "return"
            return "close"
        if item.kind == "disclosure":
            return "close" if source.state_type == StateType.DROPDOWN else "open"
        if destination.state_type in {StateType.MODAL, StateType.DROPDOWN}:
            return "open"
        if item.href or item.kind in {"link", "menuitem"}:
            return "link"
        return "control"

    async def _upsert_transition(
        self,
        source: StateMeta,
        destination: StateMeta,
        *,
        capability_id: str,
        action_type: str,
        label: str,
        selector: str,
        element_text: str | None,
        confidence: float,
        collapsed_count: int,
        via: str,
        surface_item_id: str | None,
        transition_kind: str,
        scope: str,
        reversible: bool,
        evidence: dict,
    ) -> None:
        transition_key = self._stable_hash(
            source.id,
            transition_kind,
            capability_id,
            length=40,
        )
        edge_id = self._stable_hash(self._run_id, transition_key, length=32)
        previous = self._edge_records.get(edge_id)
        created = previous is None
        previous_via = previous.get("via") if previous else None
        provenance = list(previous.get("provenance", [])) if previous else []
        provenance_value = "performed" if via in {"performed", "user_auth"} else via
        if provenance_value not in provenance:
            provenance.append(provenance_value)
        strongest_via = (
            "user_auth"
            if via == "user_auth" or previous_via == "user_auth"
            else "performed"
            if "performed" in provenance
            else "inferred"
        )
        evidence_rows = list(previous.get("evidence", [])) if previous else []
        if evidence not in evidence_rows:
            evidence_rows.append(evidence)
        effective_scope = (
            "global_navigation"
            if scope == "global_navigation"
            or (previous and previous.get("scope") == "global_navigation")
            else "local"
        )
        effective_reversible = reversible or bool(previous and previous.get("reversible"))
        # Prefer the performed action's purpose-aware label over its earlier
        # inferred placeholder, but never downgrade a performed record.
        effective_label = (
            previous.get("label", label)
            if via == "inferred" and previous_via in {"performed", "user_auth"}
            else label
        )
        record = {
            "id": edge_id,
            "transition_key": transition_key,
            "from": source.id,
            "to": destination.id,
            "action": action_type,
            "label": effective_label,
            "selector": selector,
            "element_text": element_text,
            "confidence": max(confidence, float(previous.get("confidence", 0)) if previous else 0),
            "collapsed_count": max(
                collapsed_count, int(previous.get("collapsed_count", 1)) if previous else 1
            ),
            "via": strongest_via,
            "surface_item_id": surface_item_id,
            "transition_kind": transition_kind,
            "scope": effective_scope,
            "reversible": effective_reversible,
            "provenance": provenance,
            "evidence": evidence_rows,
        }
        if previous == record:
            return
        self._edge_records[edge_id] = record

        async with self._sessions() as session:
            row = await session.get(db.Edge, edge_id)
            if row is None:
                row = db.Edge(
                    id=edge_id,
                    run_id=self._run_id,
                    from_state_id=source.id,
                    to_state_id=destination.id,
                    action_type=action_type,
                    label=effective_label,
                    selector=selector,
                    selector_strategy="css",
                )
                session.add(row)
            row.label = effective_label
            row.from_state_id = source.id
            row.to_state_id = destination.id
            row.action_type = action_type
            row.element_text = element_text
            row.confidence = record["confidence"]
            row.collapsed_count = record["collapsed_count"]
            row.via = strongest_via
            row.surface_item_id = surface_item_id
            row.transition_key = transition_key
            row.transition_kind = transition_kind
            row.scope = effective_scope
            row.reversible = effective_reversible
            row.provenance = provenance
            row.evidence = evidence_rows
            await session.commit()

        if created:
            self._stats["edges"] += 1
            if strongest_via == "inferred":
                self._stats["inferred_edges"] += 1
            if effective_scope == "global_navigation":
                self._stats["global_navigation_edges"] += 1
            if effective_reversible:
                self._stats["reversible_edges"] += 1
        else:
            if previous_via == "inferred" and strongest_via != "inferred":
                self._stats["inferred_edges"] = max(
                    0, self._stats["inferred_edges"] - 1
                )
            if (
                previous.get("scope") != "global_navigation"
                and effective_scope == "global_navigation"
            ):
                self._stats["global_navigation_edges"] += 1
            if not previous.get("reversible") and effective_reversible:
                self._stats["reversible_edges"] += 1

        self._emit(
            ExplorerEvent(
                EventType.EDGE_DISCOVERED,
                f"s{source.index} -> s{destination.index}: {effective_label}",
                {
                    "edge_id": edge_id,
                    "operation": "created" if created else "updated",
                    "from": source.id,
                    "to": destination.id,
                    "from_index": source.index,
                    "to_index": destination.index,
                    **record,
                    "counters": self._counters(),
                },
            )
        )

    async def _add_edge(
        self,
        source: StateMeta,
        destination: StateMeta,
        candidate: ActionCandidate,
        *,
        via: str = "performed",
        capability: TransitionCapability | None = None,
    ) -> None:
        item = candidate.interactable
        if via == "inferred":
            label = f"Open {item.label}"
        elif item.kind == "tab":
            label = f"Switch to {item.label}"
        elif item.kind in {"menuitem", "disclosure"} or item.href:
            label = f"Open {item.label}"
        else:
            label = f"Activate {item.label}"
        if candidate.collapsed_count > 1:
            label += f" (1 of {candidate.collapsed_count} similar)"
        capability = capability or self._capability_for(source, item)
        capability_id = (
            capability.capability_id
            if capability is not None
            else self._capability_id(source, item)
        )
        transition_kind = self._transition_kind(source, destination, item)
        reversible = transition_kind in {"close", "cancel", "back", "return"}
        scope = (
            "global_navigation"
            if capability is not None and self._is_global_capability(capability)
            else "local"
        )
        await self._upsert_transition(
            source,
            destination,
            capability_id=capability_id,
            action_type="click",
            label=label,
            selector=item.selector,
            element_text=item.text,
            confidence=max(0.0, min(1.0, candidate.score / 100.0)),
            collapsed_count=candidate.collapsed_count,
            via=via,
            surface_item_id=item.item_id or None,
            transition_kind=transition_kind,
            scope=scope,
            reversible=reversible,
            evidence={
                "mode": via,
                "surface_item_id": item.item_id or None,
                "selector": item.selector,
                "href": item.href,
                "region": item.region,
                "control_key": self._control_key(item),
                "container_key": item.container_key,
                "validated": via == "performed",
            },
        )

    def _final_status(self, meta: StateMeta, item: Interactable) -> str:
        """Terminal surface status for one item, derived from what happened."""
        outcome = self._item_outcome.get((meta.id, item.item_id))
        if outcome is not None:
            return outcome
        if item.item_id in meta.blocked_ids:
            return "blocked"
        if item.item_id not in meta.representative_ids:
            return "skipped_duplicate"  # folded into a sibling representative
        return "pending"  # ranked but never reached (budget/depth/frontier)

    async def _flush_state_details(self) -> None:
        """Write final surface-item statuses + per-state coverage at run end.

        State rows are written once at discovery time (statuses unknown then);
        this back-fills the exploration outcome so the UI and context pack can
        show explored/blocked/skipped/pending items and unexplored frontier.
        """
        pending_actions = 0
        pending_states = 0
        async with self._sessions() as session:
            for meta in self._states.values():
                counts = {
                    "explored": 0,
                    "pending": 0,
                    "blocked": 0,
                    "noop": 0,
                    "skipped_duplicate": 0,
                }
                items: list[dict] = []
                for item in meta.interactables:
                    status = self._final_status(meta, item)
                    counts[status] = counts.get(status, 0) + 1
                    payload = item.model_dump()
                    payload["status"] = status
                    items.append(payload)

                row = await session.get(db.StateNode, meta.id)
                if row is not None:
                    family = None
                    if meta.route_family is not None:
                        family = self._family_runtime_payload(meta.route_family)
                    nav_capabilities = self._nav_capabilities_for(
                        meta.auth_context, meta.interactables
                    )
                    surface_families = [
                        self._family_runtime_payload(item["pattern"])
                        for item in meta.surface_families
                        if item.get("pattern") in self._family_info
                    ]
                    existing_exploration = dict(row.exploration or {})
                    row.interactables = items
                    row.exploration = {
                        **existing_exploration,
                        **counts,
                        "nav_capabilities": nav_capabilities,
                        "surface_families": surface_families,
                        **(
                            {
                                "route_family": meta.route_family,
                                "family_sampled": len(
                                    self._family_sampled_urls.get(meta.route_family, [])
                                ),
                                "family_skipped": self._family_skipped.get(
                                    meta.route_family, 0
                                ),
                                "family": family,
                            }
                            if meta.route_family is not None
                            else {}
                        ),
                        "visit_status": (
                            "fully_explored" if counts["pending"] == 0 else "partially_explored"
                        ),
                    }
                pending_actions += counts["pending"]
                if counts["pending"] > 0:
                    pending_states += 1
            await session.commit()

        self._stats["pending_actions"] = pending_actions
        self._stats["pending_states"] = pending_states
        self._stats["surface_pending_items"] = pending_actions

    async def _finish_run(self, status: str, *, error: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self._flush_state_details()
        self._stats["actions_performed"] = self._budget.actions
        self._stats["duration_seconds"] = round(self._budget.elapsed_seconds, 2)
        self._stats["frontier_actions"] = len(self._frontier)
        stop_reason = self._stats.get("stop_reason")
        if status == "failed":
            completion_status = "failed"
        elif stop_reason == "frontier_exhausted":
            completion_status = "complete"
        elif stop_reason == "novelty_exhausted":
            completion_status = "novelty_exhausted"
        else:
            completion_status = "budget_limited"
        self._stats["completion_status"] = completion_status
        async with self._sessions() as session:
            run = await session.get(db.Run, self._run_id)
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.stats = self._stats
            if error is not None:
                run.error = error
            await session.commit()

        if status == "failed":
            self._emit(
                ExplorerEvent(
                    EventType.RUN_FAILED,
                    f"Run failed: {error}",
                    {
                        "error": error,
                        "stop_reason": stop_reason,
                        "completion_status": completion_status,
                        "stats": dict(self._stats),
                    },
                )
            )
            return

        self._emit(
            ExplorerEvent(
                EventType.RUN_COMPLETED,
                f"{status}: {self._stats['states']} states, {self._stats['edges']} edges, "
                    f"{self._stats['actions_performed']} actions "
                    f"in {self._stats['duration_seconds']}s",
                {
                    "status": status,
                    "stop_reason": stop_reason,
                    "completion_status": completion_status,
                    "stats": dict(self._stats),
                    "counters": self._counters(),
                },
            )
        )
