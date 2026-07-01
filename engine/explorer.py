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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine import identity
from engine.browser.actions import (
    click_interactable,
    click_selector,
    is_navigation_interactable,
    probe_reason,
    rebind_interactable,
    validate_interactable,
)
from engine.browser.autofill import autofill_auth_form
from engine.browser.session import BrowserSession
from engine.browser.snapshot import (
    local_probe_marker,
    stabilize,
    wait_for_local_mutation_quiet,
)
from engine.capture import (
    build_state_row,
    new_id,
    observe_page,
    persist_state_async,
    with_auth_context,
)
from engine.classify import StateAnalysis, analyze_state
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.events import ActionOutcome, EventType
from engine.families import (
    FamilyCandidate,
    FamilyRegistry,
    matches_template,
    structure_signature,
    tokenize_url,
)
from engine.identity import normalize_url
from engine.organize import heuristic_name, infer_page_role
from engine.ranking import (
    ActionCandidate,
    is_auth_entry,
    loose_url_pattern,
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
from engine.storage import LocalStorage, StorageBackend

# Stop when this many consecutive actions produce no new state (novelty collapse).
NOVELTY_PATIENCE = 30

_TERMINAL_TYPES = frozenset({StateType.EXTERNAL, StateType.DEAD_END, StateType.RISKY_TERMINAL})

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
TextEvidenceSink = Callable[[dict], Awaitable[None] | None]


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
    family_template_key: tuple | None = None
    # Bounded nearby text/structure for a future per-state LLM pass. This is
    # deliberately in-memory only and never enters persistence or SSE.
    llm_context: dict = field(default_factory=dict)


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
    # 0 auth discovery, 1 global/family navigation, 2 ordinary navigation,
    # 3 local structural probes.
    phase: int = 2
    lane: str = "navigation"
    sequence: int = 0
    required: bool = False
    obligation_kind: str | None = None
    action_key: str = ""


class Budget:
    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self.actions = 0
        self._started = time.monotonic()
        self._paused_total = 0.0
        self._pause_started: float | None = None

    def note_action(self) -> None:
        self.actions += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def auth_paused_seconds(self) -> float:
        current = (
            time.monotonic() - self._pause_started
            if self._pause_started is not None
            else 0.0
        )
        return self._paused_total + current

    @property
    def active_elapsed_seconds(self) -> float:
        return max(0.0, self.elapsed_seconds - self.auth_paused_seconds)

    def pause_for_auth(self) -> None:
        if self._pause_started is None:
            self._pause_started = time.monotonic()

    def resume_from_auth(self) -> None:
        if self._pause_started is not None:
            self._paused_total += time.monotonic() - self._pause_started
            self._pause_started = None

    def stop_reason(self, state_count: int, actions_since_new: int) -> str | None:
        if self.actions >= self._config.max_actions:
            return "max_actions"
        if state_count >= self._config.max_states:
            return "max_states"
        if self.active_elapsed_seconds >= self._config.max_wall_seconds:
            return "max_wall_seconds"
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

        candidates = [
            i for i, item in enumerate(self._items)
            if item.phase == min(candidate.phase for candidate in self._items)
        ]
        index = min(candidates, key=lambda i: priority(self._items[i]))
        item = self._items.pop(index)
        self._journey_pops[item.journey_key] = self._journey_pops.get(item.journey_key, 0) + 1
        return item

    def drain(self) -> list[PendingAction]:
        items, self._items = self._items, []
        return items

    def __len__(self) -> int:
        return len(self._items)


_AUTH_SUCCESS = re.compile(r"\b(log\s*out|sign\s*out|my\s+account|profile|dashboard)\b", re.I)
_AUTH_PATH = re.compile(
    r"/(log-?in|sign-?in|sign-?up|register|auth)(?:/|\.html?$|$)", re.I
)
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
_GENERIC_EXPANSION = re.compile(r"\b(show|see|view|load)\s+more\b|\bmore\b|\bexpand\b", re.I)


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
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        store: StorageBackend | None = None,
        text_evidence_sink: TextEvidenceSink | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._emit: EventSink = on_event or (lambda event: None)
        self._auth_gate_hook = auth_gate_hook
        self._credentials = credentials
        self._auth_mode = config.authentication.mode
        self._provided_sessions = session_factory
        self._provided_store = store
        self._text_evidence_sink = text_evidence_sink
        # Helper methods are unit-testable before a run initializes its full
        # runtime maps; the run resets this store when exploration starts.
        self._item_outcome: dict[tuple[str, str], str] = {}
        self._action_ledger: dict[str, str] = {}
        self._required_action_keys: set[str] = set()
        self._required_action_kinds: dict[str, str] = {}
        self._family_registry = FamilyRegistry(
            min_support=config.exploration.url_family_min_support,
            strong_support=config.exploration.url_family_strong_support,
            sample_cap=config.exploration.url_family_cap,
            validation_cap=config.exploration.url_family_validation_cap,
        )

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
        # Run-level URL evidence is provisional until sampled destinations
        # demonstrate compatible structure.
        family_config = self._config.exploration
        self._family_registry = FamilyRegistry(
            min_support=family_config.url_family_min_support,
            strong_support=family_config.url_family_strong_support,
            sample_cap=family_config.url_family_cap,
            validation_cap=family_config.url_family_validation_cap,
        )
        self._family_variants: dict[str, dict[tuple, str]] = {}
        self._family_sampled_urls: dict[str, list[str]] = {}
        self._family_skipped: dict[str, int] = {}
        self._family_info: dict[str, dict] = {}
        self._family_deferred: dict[str, list[PendingAction]] = defaultdict(list)
        self._auth_deferred_family_action: PendingAction | None = None
        self._auth_deferred_family_actions: dict[str, PendingAction] = {}
        self._global_probe_outcomes: dict[tuple[AuthContext, str], str] = {}
        self._edges_done: set[tuple[str, str]] = set()
        self._capabilities: dict[tuple[str, str], TransitionCapability] = {}
        self._capability_by_item: dict[tuple[str, str], str] = {}
        self._waiting_by_target: dict[tuple[AuthContext, str], set[tuple[str, str]]] = defaultdict(
            set
        )
        self._global_capabilities: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._global_targets: dict[str, str] = {}
        self._ambiguous_global_targets: set[str] = set()
        self._tab_capabilities: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        self._tab_targets: dict[tuple[str, str], str] = {}
        self._edge_records: dict[str, dict] = {}
        self._validated_history: set[tuple[str, str]] = set()
        # (state_id, item_id) -> terminal surface status for items we acted on.
        self._item_outcome: dict[tuple[str, str], str] = {}
        self._action_ledger: dict[str, str] = {}
        self._required_action_keys: set[str] = set()
        self._required_action_kinds: dict[str, str] = {}
        self._stale_rebind_attempted: set[str] = set()
        self._interaction_capabilities_seen: set[tuple[str, str, str]] = set()
        self._current_state_id: str | None = None
        self._actions_since_new = 0
        self._stats: dict = {
            "states": 0,
            "page_states": 0,
            "substates": 0,
            "interaction_nodes": 0,
            "edges": 0,
            "inferred_edges": 0,
            "dedup_hits": 0,
            "noop_actions": 0,
            "failed_actions": 0,
            "actions_denied": 0,
            "family_dedup_hits": 0,
            "family_urls_skipped": 0,
            "family_candidates_provisional": 0,
            "family_candidates_confirmed": 0,
            "family_candidates_rejected": 0,
            "family_urls_sampled": 0,
            "family_urls_deferred": 0,
            "restoration_checks": 0,
            "global_navigation_edges": 0,
            "reversible_edges": 0,
            "surface_pending_items": 0,
            "frontier_actions": 0,
            "local_probes": 0,
            "mutating_requests_blocked": 0,
            "globally_deduplicated_probes": 0,
            "auth_deferred_family_samples": 0,
            "surface_items_observed": 0,
            "interaction_capabilities": 0,
            "useful_actions": 0,
            "known_state_actions": 0,
            "stale_actions": 0,
            "replay_failed_actions": 0,
            "observed_edges": 0,
            "repeated_probes_suppressed": 0,
            "selector_collisions_resolved": 0,
            "responsive_owners_collapsed": 0,
            "navigation_links_revealed": 0,
            "pending_representative_actions": 0,
            "unresolved_discovery_obligations": 0,
        }
        self._timings: dict[str, float] = defaultdict(float)

        engine = None
        if self._provided_sessions is None:
            engine = create_db_engine(self._settings.database_url)
            await init_db(engine)
            self._sessions = create_session_factory(engine)
        else:
            self._sessions = self._provided_sessions
        self._store = self._provided_store or LocalStorage(self._settings.data_dir)

        async with self._sessions() as session:
            session.add(
                db.Run(id=self._run_id, url=url, status="running", config=self._config.model_dump())
            )
            await session.commit()

        try:
            async with BrowserSession(self._config.browser) as browser:
                self._browser_session = browser
                page = await browser.new_page()
                await self._explore(page, url)
        except Exception as exc:
            await self._finish_run("failed", error=str(exc))
            if engine is not None:
                await engine.dispose()
            raise

        await self._finish_run("done")
        if engine is not None:
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
        self._root_url = _effective_scope_url(url, page.url)

        observe_started = time.perf_counter()
        observation = await observe_page(page, self._config, auth_context=self._auth_context)
        self._timings["observe_seconds"] += time.perf_counter() - observe_started
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

        while True:
            if not len(self._frontier):
                if self._reject_unresolved_families():
                    continue
                self._stats["stop_reason"] = "frontier_exhausted"
                break
            reason = self._budget.stop_reason(len(self._states), self._actions_since_new)
            if reason:
                self._stats["stop_reason"] = reason
                break
            pending = self._frontier.pop()
            await self._execute(page, pending)
            await self._sync_surface_state(pending.from_state)
            self._emit_frontier()

    def _counters(self) -> dict:
        """UI-friendly snapshot of live progress counters."""
        return {
            "states": self._stats["states"],
            "page_states": self._stats.get("page_states", 0),
            "substates": self._stats.get("substates", 0),
            "interaction_nodes": self._stats.get("interaction_nodes", 0),
            "surface_items_observed": self._stats.get("surface_items_observed", 0),
            "interaction_capabilities": self._stats.get(
                "interaction_capabilities", 0
            ),
            "edges": self._stats["edges"],
            "inferred_edges": self._stats["inferred_edges"],
            "denied": self._stats["actions_denied"],
            "deduped": self._stats["dedup_hits"],
            "noop": self._stats["noop_actions"],
            "failed": self._stats["failed_actions"],
            "actions_performed": self._budget.actions,
            "frontier_size": len(self._frontier),
            "surface_pending": self._stats.get("surface_pending_items", 0),
            "family_provisional": self._stats.get("family_candidates_provisional", 0),
            "family_confirmed": self._stats.get("family_candidates_confirmed", 0),
            "family_rejected": self._stats.get("family_candidates_rejected", 0),
            "family_sampled": self._stats.get("family_urls_sampled", 0),
            "family_deferred": self._stats.get("family_urls_deferred", 0),
            "family_skipped": self._stats.get("family_urls_skipped", 0),
            "useful_actions": self._stats.get("useful_actions", 0),
            "known_state_actions": self._stats.get("known_state_actions", 0),
            "stale_actions": self._stats.get("stale_actions", 0),
            "replay_failed_actions": self._stats.get("replay_failed_actions", 0),
            "observed_edges": self._stats.get("observed_edges", 0),
            "repeated_probes_suppressed": self._stats.get(
                "repeated_probes_suppressed", 0
            ),
            "pending_representative_actions": self._stats.get(
                "pending_representative_actions", 0
            ),
            "unresolved_discovery_obligations": self._stats.get(
                "unresolved_discovery_obligations", 0
            ),
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

    def _emit_action_finished(self, outcome: ActionOutcome, message: str, **extra: object) -> None:
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

    @classmethod
    def _is_persistent_local_control(cls, item: Interactable) -> bool:
        if cls._is_global_chrome_item(item):
            return True
        return bool(
            item.region is None
            and re.search(
                r"\b(search|voice|account|profile|avatar)\b", item.label, re.I
            )
        )

    def _annotate_interaction_policy(self, item: Interactable) -> None:
        """Classify discovery separately from execution.

        Only real links are eligible for traversal; every other visible
        control remains useful inventory without dispatching a DOM event.
        """
        decision = evaluate_action(item, base_url=self._root_url)
        if not decision.allowed:
            item.execution_policy = "blocked"
            item.safety_category = decision.category.value if decision.category else None
            item.interaction_scope = (
                "external" if item.safety_category == "external" else "local_ui"
            )
            return
        if is_navigation_interactable(item):
            item.execution_policy = "navigate"
            item.interaction_scope = "page_navigation"
            item.safety_category = None
            return
        reason = probe_reason(item)
        if reason is not None:
            if reason == "navigation_disclosure" and (
                item.aria_expanded is True or item.aria_pressed is True
                or re.search(r"\b(show|see|view)\s+less\b|\bcollapse\b", item.label, re.I)
            ):
                # The shell is already open. Toggling it would hide routes and
                # manufacture work rather than reveal new capabilities.
                item.execution_policy = "inventory_only"
                item.interaction_scope = "local_ui"
                item.probe_reason = reason
                item.safety_category = None
                return
            item.execution_policy = "probe_local"
            item.interaction_scope = "local_ui"
            item.probe_reason = reason
            item.safety_category = None
            return
        item.execution_policy = "inventory_only"
        item.interaction_scope = "local_ui"
        item.safety_category = None

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

    def _should_materialize_inferred_edge(self, source: StateMeta, item: Interactable) -> bool:
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

    def _action_key_for(self, pending: PendingAction) -> str:
        item = pending.candidate.interactable
        return self._stable_hash(
            pending.from_state.id,
            pending.from_state.auth_context.value,
            self._control_key(item),
            item.component_key,
            item.href,
            str(item.aria_selected),
            str(item.aria_expanded),
            str(item.aria_pressed),
            str(pending.phase),
            length=32,
        )

    def _prepare_pending(self, pending: PendingAction) -> PendingAction:
        if not pending.action_key:
            pending.action_key = self._action_key_for(pending)
        if pending.required:
            self._required_action_keys.add(pending.action_key)
            self._required_action_kinds[pending.action_key] = (
                pending.obligation_kind or "required_action"
            )
        return pending

    def _record_action_outcome(self, pending: PendingAction, outcome: str) -> None:
        pending = self._prepare_pending(pending)
        previous = self._action_ledger.get(pending.action_key)
        self._action_ledger[pending.action_key] = outcome
        if previous is not None:
            return
        if outcome in {"new_state", "known_state", "explored"}:
            self._stats["useful_actions"] += 1
        if outcome == "known_state":
            self._stats["known_state_actions"] += 1
        elif outcome == "stale":
            self._stats["stale_actions"] += 1
        elif outcome == "replay_failed":
            self._stats["replay_failed_actions"] += 1

    def _required_obligation_summary(self) -> tuple[int, int]:
        unresolved = sum(
            key not in self._action_ledger for key in self._required_action_keys
        )
        failed = sum(
            self._action_ledger.get(key)
            in {"stale", "failed", "replay_failed", "blocked"}
            for key in self._required_action_keys
        )
        return unresolved, failed

    def _queue_action(self, pending: PendingAction) -> None:
        self._frontier.push(self._prepare_pending(pending))

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

    def _record_timing(self, key: str, started: float) -> None:
        self._timings[key] += time.perf_counter() - started

    @staticmethod
    def _state_evidence(observation: Observation) -> dict:
        """Actionable structure retained independently from ranking decisions."""
        substate_hints = []
        for item in observation.interactables:
            if not (
                item.kind in {"tab", "menuitem", "disclosure"}
                or item.aria_controls
                or item.aria_haspopup
                or item.aria_expanded is not None
            ):
                continue
            substate_hints.append(
                {
                    "surface_item_id": item.item_id or None,
                    "label": item.label,
                    "kind": item.kind,
                    "controls": item.aria_controls,
                    "popup": item.aria_haspopup,
                    "expanded": item.aria_expanded,
                }
            )
        return {
            **observation.snapshot.evidence,
            "substate_hints": substate_hints,
        }

    def _family_runtime_payload(self, pattern: str) -> dict:
        candidate = self._family_registry.candidates.get(pattern)
        info = (
            candidate.payload()
            if candidate is not None
            else dict(self._family_info.get(pattern, {}))
        )
        info["checked_count"] = len(self._family_sampled_urls.get(pattern, []))
        info["represented_count"] = len(self._family_variants.get(pattern, {}))
        info["variant_count"] = info["represented_count"]
        info["variant_state_ids"] = list(
            dict.fromkeys(self._family_variants.get(pattern, {}).values())
        )[:8]
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

    def _sync_family_stats(self) -> None:
        if hasattr(self, "_stats"):
            self._stats.update(self._family_registry.stats())
        for pattern, candidate in self._family_registry.candidates.items():
            if hasattr(self, "_family_sampled_urls"):
                self._family_sampled_urls[pattern] = list(candidate.samples)
            if hasattr(self, "_family_skipped"):
                self._family_skipped[pattern] = len(candidate.skipped_urls)
            if hasattr(self, "_family_info"):
                self._family_info[pattern] = candidate.payload()

    def _observe_surface_families(
        self, observation: Observation
    ) -> tuple[list[FamilyCandidate], set[str]]:
        families = self._family_registry.observe_surface(
            source_key=f"{observation.url_normalized}|{observation.fingerprint}",
            source_structure=observation.skeleton_hash,
            source_signature=structure_signature(observation),
            base_url=observation.snapshot.url,
            items=observation.interactables,
        )
        sibling_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        for item in observation.interactables:
            target = loose_url_pattern(item.href) if item.href else item.label.strip().lower()
            sibling_counts[
                (
                    item.tag,
                    item.role or "",
                    identity.strip_positional_selector(item.selector),
                    target,
                )
            ] += 1
        preserve: set[str] = set()
        for item in observation.interactables:
            if not item.href or self._is_global_chrome_item(item) or is_auth_entry(
                ActionCandidate(interactable=item)
            ):
                continue
            family = self._family_registry.family_for_url(
                item.href, base_url=observation.snapshot.url
            )
            if family is None:
                continue
            item.group_id = family.family_id
            target = loose_url_pattern(item.href) if item.href else item.label.strip().lower()
            sibling_key = (
                item.tag,
                item.role or "",
                identity.strip_positional_selector(item.selector),
                target,
            )
            if sibling_counts[sibling_key] == 1:
                preserve.add(item.item_id or item.selector)
        self._sync_family_stats()
        return families, preserve

    def _apply_surface_families(
        self,
        observation: Observation,
        analysis: StateAnalysis,
        families: list[FamilyCandidate],
    ) -> list[dict]:
        """Attach run-level candidate evidence to this state's actions."""
        if not families:
            return []

        first_for_pattern: set[str] = set()
        for candidate in analysis.candidates:
            if (
                not candidate.interactable.href
                or self._is_global_chrome_item(candidate.interactable)
                or is_auth_entry(candidate)
            ):
                continue
            family = self._family_registry.family_for_url(
                candidate.interactable.href, base_url=observation.snapshot.url
            )
            if family is None:
                continue
            candidate.family_pattern = family.pattern
            candidate.family_id = family.family_id
            candidate.family_label = family.label
            candidate.family_kind = "items"
            candidate.family_status = family.status
            if family.pattern not in first_for_pattern:
                candidate.collapsed_count = max(candidate.collapsed_count, len(family.urls))
                candidate.grouped_labels = list(
                    dict.fromkeys(item.label for item in family.evidences)
                )[:8]
                first_for_pattern.add(family.pattern)

        return [family.payload() for family in families if family.status != "rejected"]

    async def _adopt_confirmed_family(self, candidate: FamilyCandidate) -> None:
        payload = self._family_runtime_payload(candidate.pattern)
        self._family_info[candidate.pattern] = payload
        variants = self._family_variants.setdefault(candidate.pattern, {})
        anchor: StateMeta | None = None
        if candidate.family_kind == "collection_variant_family":
            for meta in self._states.values():
                exact = tokenize_url(meta.url)
                if exact is not None and exact.url in candidate.collection_anchor_urls:
                    anchor = meta
                    break
        for meta in self._states.values():
            if meta.parent_state_id is not None:
                continue
            member = self._family_registry.family_for_url(meta.url)
            if member is None or member.pattern != candidate.pattern:
                continue
            meta.route_family = candidate.pattern
            meta.family = dict(payload)
            meta.page_role = (
                PageRole.RESULTS
                if candidate.family_kind == "collection_variant_family"
                else PageRole.DETAIL
            )
            self._family_registry.record_sample_state(candidate, meta.url, meta.id)
            if meta.family_template_key is not None:
                variants.setdefault(meta.family_template_key, meta.id)
            if anchor is not None and anchor.id != meta.id and meta.parent_state_id is None:
                meta.parent_state_id = anchor.id
                meta.return_state_id = anchor.id
                meta.state_type = StateType.PAGE_VARIANT
                meta.page_depth = anchor.page_depth
                meta.substate_depth = max(1, anchor.substate_depth + 1)
                if hasattr(self, "_stats"):
                    self._stats["page_states"] = max(0, self._stats["page_states"] - 1)
                    self._stats["substates"] += 1
        payload = self._family_runtime_payload(candidate.pattern)
        self._family_info[candidate.pattern] = payload
        for meta in self._states.values():
            if meta.route_family == candidate.pattern:
                meta.family = dict(payload)
        for meta in self._states.values():
            supports_family = False
            for item in meta.interactables:
                if not item.href or self._is_global_chrome_item(item) or is_auth_entry(
                    ActionCandidate(interactable=item)
                ):
                    continue
                member = self._family_registry.family_for_url(item.href, base_url=meta.url)
                if member is not None and member.pattern == candidate.pattern:
                    item.group_id = candidate.family_id
                    supports_family = True
            if supports_family:
                meta.surface_families = [
                    item
                    for item in meta.surface_families
                    if item.get("pattern") != candidate.pattern
                ] + [dict(payload)]
                meta.page_role = PageRole.HUB

        affected = [
            meta
            for meta in self._states.values()
            if meta.route_family == candidate.pattern
            or any(item.get("pattern") == candidate.pattern for item in meta.surface_families)
        ]
        async with self._sessions() as session:
            for meta in affected:
                row = await session.get(db.StateNode, meta.id)
                if row is None:
                    continue
                row.parent_state_id = meta.parent_state_id
                row.state_type = meta.state_type.value
                row.exploration = {
                    **dict(row.exploration or {}),
                    "page_role": meta.page_role.value,
                    "page_anchor_id": meta.parent_state_id or meta.id,
                    "variant_kind": (
                        "page_variant" if meta.state_type == StateType.PAGE_VARIANT else "page"
                    ),
                    **(
                        {
                            "route_family": candidate.pattern,
                            "family": dict(meta.family or payload),
                            "family_variant_key": self._stable_hash(
                                *(str(part) for part in (meta.family_template_key or ())),
                                length=20,
                            ),
                            "family_representative_state_id": (
                                variants.get(meta.family_template_key)
                                if meta.family_template_key is not None
                                else meta.id
                            ),
                        }
                        if meta.route_family == candidate.pattern
                        else {}
                    ),
                    "surface_families": [
                        self._family_runtime_payload(item["pattern"])
                        for item in meta.surface_families
                        if item.get("pattern") in self._family_registry.candidates
                    ],
                }
            await session.commit()
        for meta in affected:
            self._emit(
                ExplorerEvent(
                    EventType.STATE_UPDATED,
                    f"Updated family metadata for s{meta.index}",
                    {
                        "state_id": meta.id,
                        "type": meta.state_type.value,
                        "parent_state_id": meta.parent_state_id,
                        "page_role": meta.page_role.value,
                        "page_anchor_id": meta.parent_state_id or meta.id,
                        "variant_kind": (
                            "page_variant"
                            if meta.state_type == StateType.PAGE_VARIANT
                            else "page"
                        ),
                        "route_family": meta.route_family,
                        "family_variant_key": (
                            self._stable_hash(
                                *(str(part) for part in (meta.family_template_key or ())),
                                length=20,
                            )
                            if meta.route_family
                            else None
                        ),
                        "family_representative_state_id": (
                            variants.get(meta.family_template_key)
                            if meta.route_family and meta.family_template_key is not None
                            else meta.id if meta.route_family else None
                        ),
                        "family": dict(meta.family or payload) if meta.route_family else None,
                        "surface_families": meta.surface_families,
                        "counters": self._counters(),
                    },
                )
            )

    def _release_family_deferred(
        self, candidate: FamilyCandidate, *, rejected: bool = False
    ) -> int:
        pending = self._family_deferred.pop(candidate.pattern, [])
        still_deferred: list[PendingAction] = []
        released = 0
        for action in pending:
            item = action.candidate.interactable
            if rejected:
                action.candidate.family_pattern = None
                action.candidate.family_id = None
                action.candidate.family_label = None
                action.candidate.family_kind = None
                action.candidate.family_status = None
                self._queue_action(action)
                released += 1
            elif self._family_registry.should_sample(candidate, item.href or ""):
                self._queue_action(action)
                released += 1
            elif candidate.status == "provisional":
                still_deferred.append(action)
            else:
                self._family_registry.mark_skipped(candidate, item.href or "")
                action = self._prepare_pending(action)
                self._edges_done.add((action.from_state.id, action.action_key))
                self._record_action_outcome(action, "known_state")
                self._item_outcome[(action.from_state.id, item.item_id)] = "skipped_duplicate"
        if still_deferred:
            self._family_deferred[candidate.pattern] = still_deferred
        self._sync_family_stats()
        return released

    def _reject_unresolved_families(self) -> int:
        released = 0
        for candidate in self._family_registry.reject_unresolved():
            released += self._release_family_deferred(candidate, rejected=True)
        self._sync_family_stats()
        return released

    def _is_global_capability(self, capability: TransitionCapability) -> bool:
        if capability.global_key is None:
            return False
        sources = {source_id for source_id, _ in self._global_capabilities[capability.global_key]}
        return len(sources) >= 2

    def _capability_for(self, source: StateMeta, item: Interactable) -> TransitionCapability | None:
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
        if target_id == source.id:
            return
        if target_id is None or target_id not in self._states:
            if capability.target_hint is not None:
                self._waiting_by_target[(source.auth_context, capability.target_hint)].add(ref)
            return
        if capability.global_key is not None and not self._should_materialize_inferred_edge(
            source, capability.item
        ):
            return

        await self._add_edge(
            source,
            self._states[target_id],
            ActionCandidate(interactable=capability.item, score=100.0),
            via="inferred",
            capability=capability,
        )

    async def _register_transition_capabilities(self, meta: StateMeta) -> None:
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

            await self._resolve_capability(ref)

        # A newly observed selected tab or newly repeated navbar signature can
        # resolve controls captured in earlier states.
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

    async def _apply_navigation_disclosure(
        self,
        source: StateMeta,
        pending: PendingAction,
        observation: Observation,
    ) -> int:
        """Merge newly revealed shell routes into their owning page state."""
        item = pending.candidate.interactable
        before = {
            (self._control_key(existing), existing.href, existing.label.strip().lower())
            for existing in source.interactables
        }
        for discovered in observation.interactables:
            self._annotate_interaction_policy(discovered)
        revealed = [
            discovered
            for discovered in observation.interactables
            if (
                self._control_key(discovered),
                discovered.href,
                discovered.label.strip().lower(),
            )
            not in before
        ]
        if not revealed:
            return 0

        dependency = {
            "selector": item.selector,
            "label": item.label,
            "role": item.role or ("link" if item.tag == "a" else "button"),
            "href": item.href,
            "control_key": self._control_key(item),
            "locator": dict(item.locator),
        }
        for discovered in revealed:
            if not discovered.href:
                discovered.dependencies = [dependency]
        source.interactables.extend(revealed)
        detected_families, preserve_ids = self._observe_surface_families(observation)
        analysis = analyze_state(
            observation,
            base_url=self._root_url,
            preserve_item_ids=preserve_ids,
        )
        self._apply_surface_families(observation, analysis, detected_families)
        self._enqueue_actions(
            source,
            analysis,
            only_item_ids={discovered.item_id for discovered in revealed},
        )
        await self._register_transition_capabilities(source)
        revealed_navigation = sum(
            discovered.execution_policy == "navigate" for discovered in revealed
        )
        self._stats["navigation_links_revealed"] += revealed_navigation
        return revealed_navigation

    async def _execute(self, page: Page, pending: PendingAction) -> None:
        pending = self._prepare_pending(pending)
        source = pending.from_state
        item = pending.candidate.interactable
        edge_key = (source.id, pending.action_key)
        if edge_key in self._edges_done:
            self._record_action_outcome(pending, "known_state")
            return
        navigation_action = (
            item.execution_policy == "navigate" and is_navigation_interactable(item)
        )
        local_probe = item.execution_policy == "probe_local" and probe_reason(item) is not None
        global_probe_key: tuple[AuthContext, str] | None = None
        if local_probe and self._is_persistent_local_control(item):
            probe_key = self._global_key(source, item, None) or self._stable_hash(
                source.auth_context.value,
                item.kind,
                item.label,
                identity.strip_positional_selector(item.selector),
                item.container_key,
                length=24,
            )
            if probe_key is not None:
                global_probe_key = (source.auth_context, probe_key)
                if global_probe_key in self._global_probe_outcomes:
                    self._stats["globally_deduplicated_probes"] += 1
                    self._stats["repeated_probes_suppressed"] += 1
                    self._item_outcome[(source.id, item.item_id)] = "skipped_duplicate"
                    self._edges_done.add(edge_key)
                    self._record_action_outcome(pending, "known_state")
                    self._emit_action_finished(
                        ActionOutcome.DEDUPED,
                        f"Reused the persistent '{item.label}' probe outcome",
                        from_state_id=source.id,
                    )
                    return
        # Defense in depth: frontier construction should already enforce this.
        if not navigation_action and not local_probe:
            self._item_outcome[(source.id, item.item_id)] = "inventory_only"
            self._edges_done.add(edge_key)
            self._record_action_outcome(pending, "noop")
            return

        # Family evidence may have accumulated after this action was queued.
        # Resolve it again at pop time so ordering is deterministic and stale
        # candidate metadata never causes an eager skip.
        family_candidate = (
            self._family_registry.family_for_url(item.href, base_url=source.url)
            if navigation_action
            and item.href
            and not self._is_global_chrome_item(item)
            and not is_auth_entry(pending.candidate)
            else None
        )
        family_pattern: str | None = None
        if family_candidate is not None:
            pending.candidate.family_pattern = family_candidate.pattern
            pending.candidate.family_id = family_candidate.family_id
            pending.candidate.family_label = family_candidate.label
            pending.candidate.family_kind = "items"
            pending.candidate.family_status = family_candidate.status
            should_sample = self._family_registry.should_sample(family_candidate, item.href or "")
            if family_candidate.status == "provisional" and not should_sample:
                self._family_deferred[family_candidate.pattern].append(pending)
                self._family_registry.mark_deferred(family_candidate, item.href or "")
                self._sync_family_stats()
                return
            if family_candidate.status == "confirmed" and not should_sample:
                self._family_registry.mark_skipped(family_candidate, item.href or "")
                self._edges_done.add(edge_key)
                self._item_outcome[(source.id, item.item_id)] = "skipped_duplicate"
                self._record_action_outcome(pending, "known_state")
                self._sync_family_stats()
                self._emit_action_finished(
                    ActionOutcome.DEDUPED,
                    f"Skipped '{item.label}' after validating route family "
                    f"{family_candidate.pattern}",
                    from_state_id=source.id,
                    route_family=family_candidate.pattern,
                )
                return
            family_pattern = family_candidate.pattern
        else:
            pending.candidate.family_pattern = None
            pending.candidate.family_id = None
            pending.candidate.family_label = None
            pending.candidate.family_kind = None
            pending.candidate.family_status = None

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
            self._item_outcome[(source.id, item.item_id)] = (
                "noop" if destination.id == source.id else "known_state"
            )
            self._record_action_outcome(
                pending, "noop" if destination.id == source.id else "known_state"
            )
            if destination.id == source.id:
                self._stats["noop_actions"] += 1
                self._emit_action_finished(
                    ActionOutcome.NOOP,
                    f"'{item.label}' is a self-link (no navigation)",
                    from_state_id=source.id,
                )
            else:
                self._stats["dedup_hits"] += 1
                await self._add_edge(source, destination, pending.candidate, via="inferred")
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
            self._item_outcome[(source.id, item.item_id)] = "known_state"
            self._record_action_outcome(pending, "known_state")
            self._emit_action_finished(
                ActionOutcome.DEDUPED,
                f"Inferred '{item.label}' -> known s{self._states[inferred_id].index}",
                from_state_id=source.id,
                to_state_id=inferred_id,
            )
            return

        self._budget.note_action()
        if local_probe:
            self._stats["local_probes"] += 1
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

        phase_started = time.perf_counter()
        direct_get = bool(
            navigation_action
            and item.href
            and is_same_origin(item.href, self._root_url)
        )
        ensured = True if direct_get else await self._ensure_at(page, source)
        self._record_timing("restore_seconds", phase_started)
        if not ensured:
            self._item_outcome[(source.id, item.item_id)] = "replay_failed"
            self._record_action_outcome(pending, "replay_failed")
            self._emit_action_finished(
                ActionOutcome.REPLAY_FAILED,
                f"Could not replay path to s{source.index}",
                from_state_id=source.id,
            )
            return

        if item.dependencies:
            try:
                for dependency in item.dependencies:
                    await click_selector(
                        page,
                        dependency.get("selector"),
                        timeout_ms=self._config.exploration.action_timeout_ms,
                        label=dependency.get("label"),
                        role=dependency.get("role"),
                        href=dependency.get("href"),
                        locator=dependency.get("locator"),
                    )
                    await stabilize(page, min(self._config.browser.stabilize_quiet_ms, 250))
            except PlaywrightError:
                self._item_outcome[(source.id, item.item_id)] = "replay_failed"
                self._record_action_outcome(pending, "replay_failed")
                self._emit_action_finished(
                    ActionOutcome.REPLAY_FAILED,
                    f"Could not restore prerequisites for {item.label!r}",
                    from_state_id=source.id,
                )
                return

        # Href navigation is performed directly and does not depend on a
        # selector surviving an authenticated-shell or hydration change.
        if not item.href and not await validate_interactable(page, item):
            if pending.action_key not in self._stale_rebind_attempted:
                self._stale_rebind_attempted.add(pending.action_key)
                rebound = await rebind_interactable(
                    page,
                    item,
                    max_elements=self._config.capture.max_interactables,
                    max_inventory_elements=self._config.capture.max_inventory_controls,
                )
                if rebound is not None:
                    self._annotate_interaction_policy(rebound)
                    pending.candidate.interactable = rebound
                    item = rebound
                    navigation_action = (
                        item.execution_policy == "navigate"
                        and is_navigation_interactable(item)
                    )
                    local_probe = (
                        item.execution_policy == "probe_local"
                        and probe_reason(item) is not None
                    )
            if not item.href and not await validate_interactable(page, item):
                self._item_outcome[(source.id, pending.candidate.interactable.item_id)] = "stale"
                self._edges_done.add(edge_key)
                self._record_action_outcome(pending, "stale")
                self._emit_action_finished(
                    ActionOutcome.STALE,
                    f"Dropped stale action {item.label!r} after semantic rebind",
                    from_state_id=source.id,
                )
                return

        blocked_before = len(self._browser_session.blocked_mutations)
        external_blocked_before = len(self._browser_session.blocked_probe_navigations)
        guarded_click = local_probe or (navigation_action and not item.href)
        if guarded_click:
            self._browser_session.set_probe_guard(True, source_url=source.url)
        local_marker_before: str | None = None
        if local_probe and item.probe_reason != "navigation_disclosure":
            with contextlib.suppress(PlaywrightError):
                local_marker_before = await local_probe_marker(page)
        if global_probe_key is not None:
            # Claim the persistent control before executing it; the frontier is
            # serial, so later copies can safely inherit this terminal outcome.
            self._global_probe_outcomes[global_probe_key] = "performed"
        try:
            phase_started = time.perf_counter()
            await click_interactable(
                page,
                item,
                timeout_ms=self._config.exploration.action_timeout_ms,
                base_url=self._root_url,
            )
            self._record_timing("action_seconds", phase_started)
        except PlaywrightError:
            if guarded_click:
                self._browser_session.set_probe_guard(False)
            self._record_timing("action_seconds", phase_started)
            self._current_state_id = None  # page state is now unknown
            self._stats["failed_actions"] += 1
            self._item_outcome[(source.id, item.item_id)] = "failed"
            self._edges_done.add(edge_key)  # don't burn budget retrying a dead selector
            self._record_action_outcome(pending, "failed")
            self._emit_action_finished(
                ActionOutcome.FAILED,
                f"Click failed on {item.label!r}",
                from_state_id=source.id,
            )
            return

        phase_started = time.perf_counter()
        blocked_after = blocked_before
        await self._absorb_popups(page)
        if local_marker_before is not None:
            local_marker_after: str | None = None
            with contextlib.suppress(PlaywrightError):
                await wait_for_local_mutation_quiet(page)
                local_marker_after = await local_probe_marker(page)
            blocked_after = len(self._browser_session.blocked_mutations)
            blocked_mutation_count = blocked_after - blocked_before
            external_blocked_count = (
                len(self._browser_session.blocked_probe_navigations)
                - external_blocked_before
            )
            if local_marker_after is not None and local_marker_after == local_marker_before:
                if guarded_click:
                    self._browser_session.set_probe_guard(False)
                self._record_timing("observe_seconds", phase_started)
                self._current_state_id = source.id
                self._edges_done.add(edge_key)
                if blocked_mutation_count > 0:
                    self._stats["mutating_requests_blocked"] += blocked_mutation_count
                    item.safety_category = "mutation_blocked"
                    self._item_outcome[(source.id, item.item_id)] = "blocked"
                    self._record_action_outcome(pending, "blocked")
                    self._emit_action_finished(
                        ActionOutcome.BLOCKED,
                        f"Blocked a mutating request from '{item.label}'",
                        from_state_id=source.id,
                    )
                elif external_blocked_count > 0:
                    item.safety_category = "external"
                    self._item_outcome[(source.id, item.item_id)] = "blocked"
                    self._record_action_outcome(pending, "blocked")
                    self._emit_action_finished(
                        ActionOutcome.BLOCKED,
                        f"Blocked external navigation from '{item.label}'",
                        from_state_id=source.id,
                    )
                else:
                    self._stats["noop_actions"] += 1
                    self._item_outcome[(source.id, item.item_id)] = "noop"
                    self._record_action_outcome(pending, "noop")
                    self._emit_action_finished(
                        ActionOutcome.NOOP,
                        f"'{item.label}' changed nothing",
                        from_state_id=source.id,
                    )
                return
        try:
            observation = await observe_page(
                page, self._config, auth_context=self._auth_context
            )
        finally:
            if guarded_click:
                self._browser_session.set_probe_guard(False)
                blocked_after = len(self._browser_session.blocked_mutations)
        self._record_timing("observe_seconds", phase_started)
        blocked_mutation_count = blocked_after - blocked_before
        if guarded_click and blocked_mutation_count > 0:
            self._stats["mutating_requests_blocked"] += blocked_mutation_count
        if item.probe_reason == "navigation_disclosure":
            revealed = await self._apply_navigation_disclosure(
                source, pending, observation
            )
            self._current_state_id = source.id
            self._item_outcome[(source.id, item.item_id)] = (
                "explored" if revealed else "noop"
            )
            self._edges_done.add(edge_key)
            if revealed:
                self._record_action_outcome(pending, "explored")
                self._emit_action_finished(
                    ActionOutcome.EXPLORED,
                    f"'{item.label}' revealed {revealed} navigation link(s)",
                    from_state_id=source.id,
                    revealed_navigation_links=revealed,
                )
            else:
                self._stats["noop_actions"] += 1
                self._record_action_outcome(pending, "noop")
                self._emit_action_finished(
                    ActionOutcome.NOOP,
                    f"'{item.label}' revealed no new navigation links",
                    from_state_id=source.id,
                )
            return
        if (
            guarded_click
            and blocked_mutation_count > 0
            and not (
                navigation_action
                and observation.url_normalized != source.url_normalized
                and is_same_origin(observation.snapshot.url, self._root_url)
            )
        ):
            item.safety_category = "mutation_blocked"
            self._item_outcome[(source.id, item.item_id)] = "blocked"
            self._edges_done.add(edge_key)
            await self._restore_probe_source(page, source)
            self._record_action_outcome(pending, "blocked")
            self._emit_action_finished(
                ActionOutcome.BLOCKED,
                f"Blocked a mutating request from '{item.label}'",
                from_state_id=source.id,
            )
            return
        if guarded_click and len(
            self._browser_session.blocked_probe_navigations
        ) > external_blocked_before:
            item.safety_category = "external"
            self._item_outcome[(source.id, item.item_id)] = "blocked"
            self._edges_done.add(edge_key)
            await self._restore_probe_source(page, source)
            self._record_action_outcome(pending, "blocked")
            self._emit_action_finished(
                ActionOutcome.BLOCKED,
                f"Blocked external navigation from '{item.label}'",
                from_state_id=source.id,
            )
            return
        if guarded_click and not is_same_origin(
            observation.snapshot.url, self._root_url
        ):
            item.safety_category = "external"
            self._item_outcome[(source.id, item.item_id)] = "blocked"
            self._edges_done.add(edge_key)
            await self._restore_probe_source(page, source)
            self._record_action_outcome(pending, "blocked")
            self._emit_action_finished(
                ActionOutcome.BLOCKED,
                f"Blocked external navigation from '{item.label}'",
                from_state_id=source.id,
            )
            return
        if (
            observation.url_normalized == source.url_normalized
            and self._is_generic_expansion(item)
            and not observation.snapshot.signals.modal_open
        ):
            self._stats["noop_actions"] += 1
            self._current_state_id = source.id
            self._item_outcome[(source.id, item.item_id)] = "noop"
            self._edges_done.add(edge_key)
            if local_probe:
                await self._restore_probe_source(page, source)
            self._record_action_outcome(pending, "noop")
            self._emit_action_finished(
                ActionOutcome.NOOP,
                f"'{item.label}' is a generic expansion, not a durable state",
                from_state_id=source.id,
            )
            return
        observed_analysis = analyze_state(observation, base_url=self._root_url)
        auth_redirect = (
            self._auth_mode == AuthMode.LOGIN
            and observed_analysis.state_type == StateType.AUTH_WALL
        )
        if family_candidate is not None and family_pattern is not None and auth_redirect:
            # Login is a deferred checkpoint for this representative, not
            # structural evidence against the route family.
            deferred_key = f"{family_candidate.pattern}|{item.href or observation.snapshot.url}"
            self._auth_deferred_family_actions[deferred_key] = pending
            self._auth_deferred_family_action = next(
                iter(self._auth_deferred_family_actions.values()), None
            )
            self._stats["auth_deferred_family_samples"] += 1
            family_pattern = None
        elif family_candidate is not None and family_pattern is not None:
            _old_status, new_status = self._family_registry.record_sample(
                family_candidate, item.href or observation.snapshot.url, observation
            )
            self._sync_family_stats()
            if new_status == "confirmed":
                await self._adopt_confirmed_family(family_candidate)
                self._release_family_deferred(family_candidate)
            elif new_status == "rejected":
                self._release_family_deferred(family_candidate, rejected=True)
            else:
                # Conflicting samples can expand the deterministic validation
                # set up to the configured cap.
                self._release_family_deferred(family_candidate)
            family_pattern = family_candidate.pattern if new_status == "confirmed" else None
        key = identity.key_for(observation)
        existing_id = self._resolve_existing_state(
            observation, source, key, local_probe=local_probe
        )
        parsed_item_url = tokenize_url(item.href or observation.snapshot.url)
        exact_family_sample = bool(
            family_candidate is not None
            and parsed_item_url is not None
            and parsed_item_url.url in family_candidate.samples
        )
        if (
            existing_id is None
            and family_pattern is not None
            and not exact_family_sample
        ):
            existing_id = self._family_template_target(observation, family_pattern)

        if existing_id == source.id:
            # The action changed nothing meaningful; don't record a self-loop.
            self._stats["noop_actions"] += 1
            self._current_state_id = source.id
            self._item_outcome[(source.id, item.item_id)] = "noop"
            self._edges_done.add(edge_key)
            if local_probe:
                await self._restore_probe_source(page, source)
            self._record_action_outcome(pending, "noop")
            self._emit_action_finished(
                ActionOutcome.NOOP,
                f"'{item.label}' changed nothing",
                from_state_id=source.id,
            )
            return

        via = "performed"
        destination_was_known = existing_id is not None
        if destination_was_known:
            destination = self._states[existing_id]
            self._stats["dedup_hits"] += 1
            if family_pattern is not None and destination.route_family == family_pattern:
                self._stats["family_dedup_hits"] += 1
            self._record_action_outcome(pending, "known_state")
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
                path=self._path_for(observation, source, item),
                key=key,
                source=source,
                trigger=item,
                route_family=family_pattern,
                journey_key=pending.journey_key,
                family=self._family_info.get(family_pattern),
                enqueue_actions=pending.phase != 0,
            )
            self._actions_since_new = 0
            self._record_action_outcome(pending, "new_state")
            self._emit_action_finished(
                ActionOutcome.NEW_STATE,
                f"'{item.label}' reached new state s{destination.index}",
                from_state_id=source.id,
                to_state_id=destination.id,
            )

        if family_candidate is not None and exact_family_sample:
            self._family_registry.record_sample_state(
                family_candidate,
                item.href or observation.snapshot.url,
                destination.id,
            )
            if family_candidate.status == "confirmed":
                await self._adopt_confirmed_family(family_candidate)
            self._sync_family_stats()

        await self._add_edge(source, destination, pending.candidate, via=via)
        await self._learn_performed_capability(source, item, destination)
        self._edges_done.add(edge_key)
        self._item_outcome[(source.id, item.item_id)] = (
            "known_state" if destination_was_known else "explored"
        )
        self._current_state_id = (
            destination.id if observation.url_normalized == destination.url_normalized else None
        )
        if self._current_state_id == destination.id and not item.href:
            await self._validate_browser_back(page, source, destination)

        if local_probe:
            if pending.phase == 0:
                self._enqueue_auth_discovery(destination, observed_analysis)
            restored = await self._restore_probe_source(page, source)
            if not restored:
                self._stats["replay_failed_actions"] += 1
                self._item_outcome[(source.id, item.item_id)] = "replay_failed"
                self._action_ledger[pending.action_key] = "replay_failed"
            return

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
        self,
        observation: Observation,
        source: StateMeta,
        key: identity.StateKey,
        *,
        local_probe: bool = False,
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
        if local_probe and self._same_route(observation.snapshot.url, source.url):
            return None
        if observation.snapshot.signals.modal_open:
            return None
        existing = self._url_to_state.get((observation.auth_context, observation.url_normalized))
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

    def _family_template_target(self, observation: Observation, family_pattern: str) -> str | None:
        """Match only exact structure/affordances within an inferred family."""
        candidate = self._family_registry.candidates.get(family_pattern)
        if candidate is None or candidate.status != "confirmed":
            return None
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
    def _same_route(left: str, right: str) -> bool:
        """Whether two URLs represent variants of one page route.

        Query-only changes produced by filters/sorts belong to the page's
        interaction layer, while a path change remains page navigation.
        """
        a, b = urlsplit(left), urlsplit(right)
        return (
            a.scheme.lower(),
            a.netloc.lower(),
            a.path.rstrip("/") or "/",
        ) == (
            b.scheme.lower(),
            b.netloc.lower(),
            b.path.rstrip("/") or "/",
        )

    @staticmethod
    def _substate_type(trigger: Interactable | None) -> StateType:
        """Subtype for a same-URL state change that isn't a modal."""
        if trigger is not None:
            if trigger.role == "tab" or trigger.kind == "tab":
                return StateType.PAGE_VARIANT
            label = trigger.label.lower()
            if (
                trigger.tag == "summary"
                or trigger.kind in ("disclosure", "menuitem")
                or trigger.aria_expanded is not None
                or re.search(r"\b(menu|settings?|account|profile)\b", label)
            ):
                return StateType.DROPDOWN
            if (
                trigger.kind in {"select", "toggle", "search"}
                or trigger.probe_reason in {
                    "focus_search",
                    "labelled_structural_button",
                    "table_control",
                }
                or re.search(
                    r"\b(filter|sort|search|chart|metrics?|table|categor(?:y|ies)|layout|view)\b",
                    label,
                )
            ):
                return StateType.PAGE_VARIANT
        return StateType.DROPDOWN

    @staticmethod
    def _has_page_variant_intent(trigger: Interactable | None) -> bool:
        if trigger is None:
            return False
        if trigger.kind in {"tab", "select", "toggle", "search"}:
            return True
        return bool(
            re.search(
                r"\b(filter|sort|search|chart|metrics?|table|categor(?:y|ies)|layout|view)\b",
                trigger.label,
                re.I,
            )
        )

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
        for item in observation.interactables:
            self._annotate_interaction_policy(item)
        detected_families, preserve_item_ids = self._observe_surface_families(observation)
        analysis = analyze_state(
            observation,
            base_url=self._root_url,
            preserve_item_ids=preserve_item_ids,
        )
        surface_families = self._apply_surface_families(observation, analysis, detected_families)
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
        elif source is not None and (
            observation.url_normalized == source.url_normalized
            or (
                trigger is not None
                and trigger.execution_policy == "probe_local"
                and self._same_route(observation.snapshot.url, source.url)
            )
            or (
                self._has_page_variant_intent(trigger)
                and (
                    self._same_route(observation.snapshot.url, source.url)
                    or self._family_template_key(observation)
                    == source.family_template_key
                )
            )
        ):
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

        if route_family is not None:
            candidate = self._family_registry.candidates.get(route_family)
            if candidate is None or candidate.status != "confirmed":
                route_family = None
                family = None
            else:
                family = candidate.payload()
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
        state_evidence = self._state_evidence(observation)

        phase_started = time.perf_counter()
        state = await persist_state_async(
            observation,
            run_id=self._run_id,
            state_id=new_id(),
            depth=depth,
            path=path,
            store=self._store,
            save_dom=self._config.capture.save_dom_snapshots,
        )
        self._record_timing("artifact_write_seconds", phase_started)
        state.state_type = state_type
        state.detected_flags = {
            **analysis.flags,
            "auth_context": observation.auth_context.value,
            "page_role": page_role.value,
            "name": naming,
        }

        phase_started = time.perf_counter()
        async with self._sessions() as session:
            row = build_state_row(state)
            row.parent_state_id = parent_state_id
            row.label = naming["text"]
            row.exploration = {
                "auth_context": observation.auth_context.value,
                "route_surface_key": self._stable_hash(
                    observation.auth_context.value,
                    observation.url_normalized,
                    length=24,
                ),
                "page_anchor_id": parent_state_id or state.state_id,
                "variant_kind": (
                    "page"
                    if parent_state_id is None
                    else "page_variant"
                    if state_type in {StateType.PAGE_VARIANT, StateType.TAB}
                    else "overlay"
                ),
                **(
                    {"inherited_surface_state_id": parent_state_id}
                    if parent_state_id is not None
                    else {}
                ),
                "page_role": page_role.value,
                "page_depth": page_depth,
                "substate_depth": substate_depth,
                "nav_capabilities": nav_capabilities,
                "surface_families": surface_families,
                "evidence": state_evidence,
                **({"return_state_id": return_state_id} if return_state_id else {}),
                "name": naming,
                **({"route_family": route_family} if route_family else {}),
                **({"family": dict(family)} if family else {}),
            }
            session.add(row)
            await session.commit()
        self._record_timing("state_db_seconds", phase_started)

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
            family_template_key=self._family_template_key(observation),
            llm_context={
                "url": observation.snapshot.url,
                "title": observation.snapshot.title,
                **observation.snapshot.text_evidence,
                "controls": [
                    {
                        "item_id": item.item_id,
                        "label": item.label,
                        "component": item.component_label,
                        "context": item.context_label,
                        "icon": item.icon_label,
                        "role": item.role,
                        "kind": item.kind,
                        "controls": item.aria_controls,
                    }
                    for item in observation.interactables[:100]
                ],
            },
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
        if parent_state_id is None:
            self._stats["page_states"] += 1
        else:
            self._stats["substates"] += 1
        self._stats["surface_items_observed"] += len(meta.interactables)
        for item in meta.interactables:
            if item.interaction_scope == "page_navigation":
                continue
            self._interaction_capabilities_seen.add(
                (
                    observation.auth_context.value,
                    observation.url_normalized,
                    item.control_key or item.item_id,
                )
            )
            if item.locator.get("duplicate_id_ancestor"):
                self._stats["selector_collisions_resolved"] += 1
            self._stats["responsive_owners_collapsed"] += int(
                item.locator.get("responsive_alias_count", 0)
            )
        self._stats["interaction_capabilities"] = len(
            self._interaction_capabilities_seen
        )
        self._stats["interaction_nodes"] = self._stats["interaction_capabilities"]
        self._stats["actions_denied"] += len(analysis.denied)
        self._stats["denied_actions"] = self._stats["actions_denied"]

        if self._text_evidence_sink is not None:
            result = self._text_evidence_sink(
                {
                    "state_id": meta.id,
                    "url": observation.snapshot.url,
                    "title": observation.snapshot.title,
                    "visible_text": observation.snapshot.visible_text,
                    **observation.snapshot.text_evidence,
                }
            )
            if result is not None:
                await result

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
                    "route_surface_key": row.exploration.get("route_surface_key"),
                    "page_anchor_id": row.exploration.get("page_anchor_id"),
                    "variant_kind": row.exploration.get("variant_kind"),
                    "inherited_surface_state_id": row.exploration.get(
                        "inherited_surface_state_id"
                    ),
                    "auth_context": observation.auth_context.value,
                    "page_depth": page_depth,
                    "substate_depth": substate_depth,
                    "return_state_id": return_state_id,
                    "family": family,
                    "nav_capabilities": nav_capabilities,
                    "surface_families": surface_families,
                    "evidence": state_evidence,
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

    def _surface_summary(self, meta: StateMeta) -> list[dict]:
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
                "aria_controls": item.aria_controls,
                "aria_haspopup": item.aria_haspopup,
                "aria_pressed": item.aria_pressed,
                "checked": item.checked,
                "tag": item.tag,
                "role": item.role,
                "href": item.href,
                "placeholder": item.placeholder,
                "name": item.name,
                "associated_label": item.associated_label,
                "input_type": item.input_type,
                "required": item.required,
                "autocomplete": item.autocomplete,
                "form_action": item.form_action,
                "form_method": item.form_method,
                "control_key": item.control_key,
                "container_key": item.container_key,
                "container_type": item.container_type,
                "controlled_surface": item.controlled_surface,
                "component_key": item.component_key,
                "component_label": item.component_label,
                "icon_label": item.icon_label,
                "probe_reason": item.probe_reason,
                "interaction_scope": item.interaction_scope,
                "execution_policy": item.execution_policy,
                "safety_category": item.safety_category,
                "page_box": (
                    item.page_box.model_dump() if item.page_box is not None else None
                ),
                "status": self._final_status(meta, item),
            }
            for item in meta.interactables
        ]

    def _path_for(
        self,
        observation: Observation,
        source: StateMeta,
        item: Interactable,
    ) -> list[ActionStep]:
        """Replay path for a new state: URL-addressable states restart from a
        goto; sub-URL states (modals, tabs) extend the source state's path."""
        if (
            observation.url_normalized != source.url_normalized
            and not observation.snapshot.signals.modal_open
            and not (
                item.execution_policy == "probe_local"
                and self._same_route(observation.snapshot.url, source.url)
            )
        ):
            return [ActionStep(kind="goto", url=observation.snapshot.url)]
        return [
            *source.path,
            ActionStep(
                kind="click",
                selector=item.selector,
                label=item.label,
                role=item.role or ("link" if item.tag == "a" else "button"),
                href=item.href,
                control_key=item.control_key or None,
                locator=dict(item.locator),
            ),
        ]

    def _enqueue_actions(
        self,
        meta: StateMeta,
        analysis: StateAnalysis,
        *,
        only_item_ids: set[str] | None = None,
    ) -> None:
        if meta.state_type in _TERMINAL_TYPES:
            meta.representative_ids = set()
            return
        if meta.state_type in _GATE_TYPES:
            meta.representative_ids = set()
            return  # expanded only after auth_gate resolution via _handle_auth_wall
        allow_navigation = meta.page_depth < self._config.budgets.max_depth
        allow_local = meta.substate_depth < self._config.exploration.max_substate_depth
        eligible: list[ActionCandidate] = []
        local_eligible: list[ActionCandidate] = []
        parent_control_keys: set[str] = set()
        if meta.parent_state_id:
            parent = self._states.get(meta.parent_state_id)
            if parent is not None:
                parent_control_keys = {
                    self._control_key(item)
                    for item in parent.interactables
                }
        seen_semantic_actions: set[tuple[str, str | None]] = set()
        click_only_semantic_counts: dict[tuple[str, str | None], int] = defaultdict(int)
        # Prefer a real href owner, then a custom semantic link, over an
        # overlapping empty visual anchor with the same label.
        discovery_order = sorted(
            (
                candidate
                for candidate in analysis.safe
                if only_item_ids is None
                or candidate.interactable.item_id in only_item_ids
            ),
            key=lambda candidate: (
                2
                if candidate.interactable.href
                else 1
                if candidate.interactable.role == "link"
                and candidate.interactable.tag != "a"
                else 0
            ),
            reverse=True,
        )
        for candidate in discovery_order:
            item = candidate.interactable
            if item.execution_policy == "probe_local":
                if not allow_local:
                    self._item_outcome[(meta.id, item.item_id)] = "inventory_only"
                    continue
                if (
                    parent_control_keys
                    and self._control_key(item) in parent_control_keys
                    and not item.in_modal
                    and item.region != "modal"
                ):
                    self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                    continue
                candidate.score = score_action(candidate, visited_urls=self._visited_urls)
                if item.aria_controls or item.aria_haspopup or item.aria_expanded is not None:
                    candidate.score += 20
                local_eligible.append(candidate)
                continue
            if item.execution_policy != "navigate" or not allow_navigation:
                self._item_outcome[(meta.id, item.item_id)] = "inventory_only"
                continue
            target_hint = self._target_hint(item)
            known_target = (
                self._known_target_id(meta.auth_context, target_hint)
                if target_hint is not None
                else None
            )
            if target_hint == meta.url_normalized:
                self._item_outcome[(meta.id, item.item_id)] = "noop"
                continue
            if self._is_peripheral_action(item) and not is_auth_entry(candidate):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if (
                self._is_global_chrome_item(item)
                and meta.page_depth > 0
                and meta.page_role != PageRole.HOME
                and not is_auth_entry(candidate)
            ):
                self._item_outcome[(meta.id, item.item_id)] = "skipped_duplicate"
                continue
            if meta.auth_context == AuthContext.GUEST and re.search(
                r"\bsettings?\b", item.label, re.I
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
            if item.label.lower().startswith("unlabelled ") and item.region not in {
                "nav",
                "header",
                "modal",
            }:
                continue
            semantic_key = (self._control_key(item), item.href)
            if (
                semantic_key in seen_semantic_actions
                and candidate.family_pattern is None
                and not (
                    item.execution_policy == "navigate"
                    and not item.href
                    and click_only_semantic_counts[semantic_key] < 2
                )
            ):
                continue
            seen_semantic_actions.add(semantic_key)
            if item.execution_policy == "navigate" and not item.href:
                click_only_semantic_counts[semantic_key] += 1
            if (
                parent_control_keys
                and self._control_key(item) in parent_control_keys
                and not (
                    (item.kind == "tab" and meta.state_type == StateType.TAB)
                    or item.in_modal
                    or item.region == "modal"
                )
            ):
                # Unchanged navigation/footer chrome belongs to the parent.
                continue
            candidate.score = score_action(candidate, visited_urls=self._visited_urls)
            eligible.append(candidate)
        ranked = sorted(eligible, key=lambda c: c.score, reverse=True)
        local_ranked = sorted(local_eligible, key=lambda c: c.score, reverse=True)
        local_top = local_ranked[: self._config.exploration.max_local_actions_per_state]
        nav_cap = self._config.exploration.max_actions_per_state
        top = list(ranked) if nav_cap is None else ranked[:nav_cap]
        # Always reserve a slot for a sign-in/sign-up affordance when one exists.
        auth_pick = next((c for c in ranked if is_auth_entry(c)), None)
        if auth_pick and auth_pick not in top:
            top = [auth_pick, *[c for c in top if c is not auth_pick]]
            if nav_cap is not None:
                top = top[:nav_cap]
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
                top = [return_pick, *top]
                if nav_cap is not None:
                    top = top[:nav_cap]
        else:
            return_pick = None

        # Structural validation needs the registry's deterministic samples,
        # even when ordinary ranking would place filters or chrome above them.
        sample_picks = []
        for candidate in ranked:
            item = candidate.interactable
            family = (
                self._family_registry.family_for_url(item.href, base_url=meta.url)
                if item.href
                and not self._is_global_chrome_item(item)
                and not is_auth_entry(candidate)
                else None
            )
            if family is not None and self._family_registry.should_sample(
                family, item.href or ""
            ):
                sample_picks.append(candidate)
        required = [*sample_picks]
        if meta.page_depth == 0:
            global_picks = [
                candidate for candidate in ranked
                if self._is_global_chrome_item(candidate.interactable)
            ]
            global_cap = self._config.exploration.max_global_navigation_actions
            if global_cap is not None:
                global_picks = global_picks[:global_cap]
            required = [*global_picks, *required]
        if auth_pick is not None:
            required.insert(0, auth_pick)
        if return_pick is not None:
            required.insert(0, return_pick)
        ordered: list[ActionCandidate] = []
        for candidate in [*required, *top, *ranked]:
            if candidate not in ordered:
                ordered.append(candidate)
        top = (
            ordered
            if nav_cap is None
            else ordered[: max(nav_cap, len(required))]
        )

        queued_ids = {c.interactable.item_id for c in [*top, *local_top]}
        deferred_ids: set[str] = set()
        for candidate in eligible:
            item_id = candidate.interactable.item_id
            if item_id not in queued_ids:
                item = candidate.interactable
                family = (
                    self._family_registry.family_for_url(item.href, base_url=meta.url)
                    if item.href
                    and not self._is_global_chrome_item(item)
                    and not is_auth_entry(candidate)
                    else None
                )
                if family is not None and family.status == "provisional":
                    journey_key = (
                        _journey_slug(candidate.interactable.label)
                        if meta.page_depth == 0
                        else meta.journey_key
                    )
                    action = self._prepare_pending(PendingAction(
                        from_state=meta,
                        candidate=candidate,
                        journey_key=journey_key,
                        phase=1,
                        required=self._family_registry.should_sample(
                            family, candidate.interactable.href or ""
                        ),
                        obligation_kind="family_representative",
                    ))
                    self._family_deferred[family.pattern].append(action)
                    self._family_registry.mark_deferred(family, candidate.interactable.href or "")
                    deferred_ids.add(item_id)
                else:
                    if family is not None and family.status == "confirmed":
                        self._family_registry.mark_skipped(
                            family, candidate.interactable.href or ""
                        )
                    self._item_outcome.setdefault((meta.id, item_id), "skipped_duplicate")
        if only_item_ids is None:
            meta.representative_ids = queued_ids | deferred_ids
        else:
            meta.representative_ids.update(queued_ids | deferred_ids)
        self._sync_family_stats()
        for candidate in top:
            journey_key = meta.journey_key
            if meta.page_depth == 0:
                journey_key = _journey_slug(candidate.interactable.label)
            item = candidate.interactable
            family = (
                self._family_registry.family_for_url(item.href, base_url=meta.url)
                if item.href and not self._is_global_chrome_item(item)
                else None
            )
            family_sample = bool(
                family is not None
                and self._family_registry.should_sample(family, item.href or "")
            )
            global_required = self._is_global_chrome_item(item)
            self._queue_action(
                PendingAction(
                    from_state=meta,
                    candidate=candidate,
                    journey_key=journey_key,
                    phase=(
                        1
                        if self._is_global_chrome_item(candidate.interactable)
                        or (
                            candidate.interactable.href
                            and (
                                family := self._family_registry.family_for_url(
                                    candidate.interactable.href, base_url=meta.url
                                )
                            ) is not None
                            and self._family_registry.should_sample(
                                family, candidate.interactable.href
                            )
                        )
                        else 2
                    ),
                    required=global_required or family_sample,
                    obligation_kind=(
                        "family_representative" if family_sample else "primary_navigation"
                        if global_required else None
                    ),
                )
            )
        for candidate in local_top:
            disclosure = candidate.interactable.probe_reason == "navigation_disclosure"
            self._queue_action(
                PendingAction(
                    from_state=meta,
                    candidate=candidate,
                    journey_key=meta.journey_key,
                    phase=1 if disclosure else 3,
                    lane="disclosure" if disclosure else "local",
                    required=disclosure,
                    obligation_kind="navigation_disclosure" if disclosure else None,
                )
            )

    def _enqueue_auth_discovery(self, meta: StateMeta, analysis: StateAnalysis) -> None:
        """Seed login mode with auth actions before any general journey."""
        ranked: list[ActionCandidate] = []
        revealers: list[ActionCandidate] = []
        for candidate in analysis.safe:
            item = candidate.interactable
            candidate.score = score_action(candidate, visited_urls=self._visited_urls)
            if item.execution_policy == "navigate" and is_auth_entry(candidate):
                ranked.append(candidate)
            elif item.execution_policy == "probe_local":
                label = item.label.lower()
                if (
                    item.region in {"nav", "header"}
                    and item.kind in {"button", "menuitem", "disclosure"}
                    and any(
                        word in label
                        for word in ("menu", "account", "profile", "avatar", "user")
                    )
                ):
                    revealers.append(candidate)
            else:
                self._item_outcome.setdefault(
                    (meta.id, item.item_id), "inventory_only"
                )
        def auth_priority(candidate: ActionCandidate) -> tuple[int, float]:
            path = urlsplit(candidate.interactable.href or "").path.lower()
            # Map registration/recovery boundaries before entering the login
            # gate, otherwise authentication replaces the guest header.
            registration = bool(
                re.search(
                    r"/(sign-?up|register|create-account)(?:/|\.html?$|$)", path
                )
            )
            recovery = bool(
                re.search(r"/(forgot|recover|reset-password)(?:/|\.html?$|$)", path)
            )
            return (0 if registration or recovery else 1, -candidate.score)

        picks = sorted(ranked, key=auth_priority)
        if not picks:
            picks = sorted(revealers, key=lambda c: c.score, reverse=True)[
                : self._config.exploration.auth_discovery_action_cap
            ]
        # Frontier score is otherwise allowed to reorder one auth boundary
        # ahead of another. Preserve registration/recovery-before-login order.
        for index, candidate in enumerate(picks):
            candidate.score = 10_000.0 - index
        meta.representative_ids = {c.interactable.item_id for c in picks}
        for candidate in picks:
            item = candidate.interactable
            path = urlsplit(item.href or "").path.lower()
            maps_guest_auth_boundary = bool(
                re.search(
                    r"/(sign-?up|register|create-account|forgot|recover|reset-password)"
                    r"(?:/|\.html?$|$)",
                    path,
                )
            )
            self._queue_action(
                PendingAction(
                    from_state=meta,
                    candidate=candidate,
                    journey_key="authentication",
                    phase=(
                        0
                        if maps_guest_auth_boundary
                        or item.execution_policy == "probe_local"
                        else 1
                    ),
                    lane=(
                        "local"
                        if item.execution_policy == "probe_local"
                        else "navigation"
                    ),
                    required=True,
                    obligation_kind="auth_discovery",
                )
            )

        # If no authentication entry can be found, pause at the root instead
        # of silently pretending this is an authenticated run.
        if not picks:
            self._enqueue_actions(meta, analysis)

    # ------------------------------------------------------------------
    # Auth gate
    # ------------------------------------------------------------------

    async def _handle_auth_wall(self, page: Page, meta: StateMeta) -> StateMeta | None:
        """Authenticate, retrying the checkpoint until success or explicit skip."""
        autofill_attempted = False
        resume_authorized = False
        while True:
            observation: Observation | None = None
            autofill_submitted = False
            if self._credentials is not None and resume_authorized:
                autofill_attempted = True
                resume_authorized = False
                try:
                    auth_page_url = page.url
                    self._browser_session.set_auth_submission_allowed(True)
                    try:
                        submitted = await autofill_auth_form(
                            page,
                            self._credentials,
                            timeout_ms=self._config.exploration.action_timeout_ms,
                        )
                    finally:
                        self._browser_session.set_auth_submission_allowed(False)
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
                    observation = await observe_page(
                        page, self._config, auth_context=AuthContext.GUEST
                    )
                except Exception:  # noqa: BLE001 - user can recover at the gate
                    observation = None

            if observation is not None and self._auth_succeeded(observation, meta):
                return await self._register_authenticated_state(page, observation, meta)

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

            budget = getattr(self, "_budget", None)
            if budget is not None:
                budget.pause_for_auth()
            try:
                decision, new_credentials = await self._auth_gate_hook(meta.id, meta.url)
            finally:
                if budget is not None:
                    budget.resume_from_auth()
            if new_credentials is not None:
                self._credentials = new_credentials
            if decision == "skip":
                self._auth_mode = AuthMode.GUEST
                self._config.authentication.mode = AuthMode.GUEST
                await self._mark_auth_skipped(meta.id)
                return None

            # A headed run may have been authenticated manually while paused.
            try:
                observation = await observe_page(page, self._config, auth_context=AuthContext.GUEST)
            except Exception:  # noqa: BLE001
                observation = None
            if observation is not None and self._auth_succeeded(observation, meta):
                return await self._register_authenticated_state(page, observation, meta)
            resume_authorized = True
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
        self, page: Page, observation: Observation, source: StateMeta
    ) -> StateMeta:
        self._auth_context = AuthContext.AUTHENTICATED
        observation = with_auth_context(observation, AuthContext.AUTHENTICATED)
        # Hold the guest frontier while the authenticated shell is rebuilt.
        # Its direct route targets are rebased afterwards instead of discarded.
        guest_pending = self._frontier.drain()
        self._frontier = Frontier()

        deferred_actions = list(self._auth_deferred_family_actions.values())
        if not deferred_actions and self._auth_deferred_family_action is not None:
            deferred_actions = [self._auth_deferred_family_action]
        self._auth_deferred_family_actions.clear()
        self._auth_deferred_family_action = None
        route_family: str | None = None
        matched_deferred: tuple[PendingAction, FamilyCandidate] | None = None
        for deferred in deferred_actions:
            item = deferred.candidate.interactable
            candidate = (
                self._family_registry.family_for_url(item.href, base_url=deferred.from_state.url)
                if item.href
                else None
            )
            if (
                candidate is not None
                and item.href
                and matches_template(item.href, candidate.pattern)
                and matches_template(observation.snapshot.url, candidate.pattern)
            ):
                _old_status, new_status = self._family_registry.record_sample(
                    candidate, item.href, observation
                )
                self._sync_family_stats()
                if new_status == "confirmed":
                    await self._adopt_confirmed_family(candidate)
                    self._release_family_deferred(candidate)
                    route_family = candidate.pattern
                elif new_status == "rejected":
                    self._release_family_deferred(candidate, rejected=True)
                else:
                    self._release_family_deferred(candidate)
                matched_deferred = (deferred, candidate)
            else:
                # The provider ignored its return target. Retry this safe GET
                # route after reseeding instead of treating it as divergence.
                guest_pending.append(deferred)

        # Family validation may have released more guest actions.
        guest_pending.extend(self._frontier.drain())
        self._frontier = Frontier()
        returned_to_root = self._same_route(observation.snapshot.url, self._root_url)
        post_auth = await self._register_state(
            observation,
            depth=source.depth + 1,
            path=[ActionStep(kind="goto", url=observation.snapshot.url)],
            key=identity.key_for(observation),
            source=source,
            route_family=route_family,
            journey_key="authentication",
            page_depth_override=0 if returned_to_root else None,
        )
        if matched_deferred is not None:
            deferred_action, candidate = matched_deferred
            self._family_registry.record_sample_state(
                candidate,
                deferred_action.candidate.interactable.href or observation.snapshot.url,
                post_auth.id,
            )
            if candidate.status == "confirmed":
                await self._adopt_confirmed_family(candidate)
            self._record_action_outcome(deferred_action, "new_state")

        auth_root = post_auth
        if not returned_to_root:
            await page.goto(self._root_url)
            root_observation = await observe_page(
                page, self._config, auth_context=AuthContext.AUTHENTICATED
            )
            root_key = identity.key_for(root_observation)
            existing_root_id = self._identity.find(root_key)
            if existing_root_id is None:
                auth_root = await self._register_state(
                    root_observation,
                    depth=post_auth.depth + 1,
                    path=[ActionStep(kind="goto", url=root_observation.snapshot.url)],
                    key=root_key,
                    source=post_auth,
                    journey_key="root",
                    page_depth_override=0,
                )
            else:
                auth_root = self._states[existing_root_id]
            if auth_root.id != post_auth.id:
                await self._add_auth_reseed_edge(post_auth, auth_root)

        auth_root_hrefs = {
            normalize_url(item.href)
            for item in auth_root.interactables
            if item.href and is_same_origin(item.href, self._root_url)
        }
        rebased_urls: set[str] = set()
        for pending in guest_pending:
            item = pending.candidate.interactable
            self._item_outcome.setdefault(
                (pending.from_state.id, item.item_id), "skipped_duplicate"
            )
            if (
                item.execution_policy != "navigate"
                or not item.href
                or not is_same_origin(item.href, self._root_url)
                or is_auth_entry(pending.candidate)
                or self._is_peripheral_action(item)
            ):
                continue
            target = normalize_url(item.href)
            if target in auth_root_hrefs or target in rebased_urls:
                continue
            rebased_urls.add(target)
            self._queue_action(
                PendingAction(
                    from_state=auth_root,
                    candidate=pending.candidate,
                    journey_key=pending.journey_key,
                    phase=1 if self._is_global_chrome_item(item) else pending.phase,
                    required=pending.required,
                    obligation_kind=pending.obligation_kind,
                )
            )

        # Rebase still-provisional family representatives as well; they are
        # not currently in the frontier but must keep their sampling schedule.
        for pattern, actions in list(self._family_deferred.items()):
            self._family_deferred[pattern] = [
                PendingAction(
                    from_state=auth_root,
                    candidate=action.candidate,
                    journey_key=action.journey_key,
                    phase=1,
                    required=action.required,
                    obligation_kind=action.obligation_kind,
                )
                for action in actions
            ]
        self._actions_since_new = 0
        self._current_state_id = auth_root.id
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

    async def _add_user_auth_edge(self, source: StateMeta, destination: StateMeta) -> None:
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

    async def _add_auth_reseed_edge(
        self, source: StateMeta, destination: StateMeta
    ) -> None:
        """Record the deliberate authenticated-root revisit."""
        await self._upsert_transition(
            source,
            destination,
            capability_id="auth-reseed",
            action_type="auth_reseed",
            label="Revisit authenticated root",
            selector="auth:reseed",
            element_text=None,
            confidence=1.0,
            collapsed_count=1,
            via="auth_reseed",
            surface_item_id=None,
            transition_kind="auth_reseed",
            scope="global",
            reversible=False,
            evidence={"mode": "auth_reseed", "validated": True},
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
                        label=step.label,
                        role=step.role,
                        href=step.href,
                        locator=step.locator,
                    )
                await stabilize(page, self._config.browser.stabilize_quiet_ms)
        except PlaywrightError:
            self._current_state_id = None
            return False
        self._current_state_id = target.id
        return True

    async def _restore_probe_source(self, page: Page, source: StateMeta) -> bool:
        """Replay the exact source after a bounded local-control probe."""
        self._current_state_id = None
        return await self._ensure_at(page, source)

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
            restored = await observe_page(page, self._config, auth_context=self._auth_context)
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
    def _transition_kind(source: StateMeta, destination: StateMeta, item: Interactable) -> str:
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
            "selector": previous.get("selector", selector) if previous else selector,
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

        phase_started = time.perf_counter()
        async with self._sessions() as session:
            row = await session.get(db.Edge, edge_id)
            if row is None:
                # Graph-v4 uses ``transition_key`` as the semantic identity,
                # but existing databases retain the original selector-based
                # uniqueness constraint. Two distinct controls can legitimately
                # reuse one generated CSS selector after hydration. Preserve
                # both semantic edges with a deterministic, CSS-valid comment
                # discriminator instead of letting that compatibility index
                # abort the entire run.
                storage_selector = selector
                legacy_collision = (
                    await session.execute(
                        select(db.Edge).where(
                            db.Edge.run_id == self._run_id,
                            db.Edge.from_state_id == source.id,
                            db.Edge.selector == selector,
                            db.Edge.action_type == action_type,
                        )
                    )
                ).scalar_one_or_none()
                if legacy_collision is not None:
                    storage_selector = (
                        f"{selector}/*flowstate-edge:{transition_key[:12]}*/"
                    )
                row = db.Edge(
                    id=edge_id,
                    run_id=self._run_id,
                    from_state_id=source.id,
                    to_state_id=destination.id,
                    action_type=action_type,
                    label=effective_label,
                    selector=storage_selector,
                    selector_strategy="css",
                )
                session.add(row)
                record["selector"] = storage_selector
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
        self._edge_records[edge_id] = record
        self._record_timing("edge_db_seconds", phase_started)

        if created:
            self._stats["edges"] += 1
            if strongest_via == "inferred":
                self._stats["inferred_edges"] += 1
            else:
                self._stats["observed_edges"] += 1
            if effective_scope == "global_navigation":
                self._stats["global_navigation_edges"] += 1
            if effective_reversible:
                self._stats["reversible_edges"] += 1
        else:
            if previous_via == "inferred" and strongest_via != "inferred":
                self._stats["inferred_edges"] = max(0, self._stats["inferred_edges"] - 1)
                self._stats["observed_edges"] += 1
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
        if item.execution_policy == "blocked" or item.item_id in meta.blocked_ids:
            return "blocked"
        if item.execution_policy == "inventory_only":
            return "inventory_only"
        if item.item_id not in meta.representative_ids:
            return "skipped_duplicate"  # folded into a sibling representative
        return "pending"  # ranked but never reached (budget/depth/frontier)

    def _surface_details(self, meta: StateMeta) -> tuple[list[dict], dict[str, int]]:
        counts = {
            "explored": 0,
            "pending": 0,
            "blocked": 0,
            "noop": 0,
            "skipped_duplicate": 0,
            "inventory_only": 0,
            "known_state": 0,
            "stale": 0,
            "failed": 0,
            "replay_failed": 0,
        }
        items: list[dict] = []
        for item in meta.interactables:
            status = self._final_status(meta, item)
            counts[status] = counts.get(status, 0) + 1
            payload = item.model_dump()
            payload["status"] = status
            items.append(payload)
        return items, counts

    async def _sync_surface_state(self, meta: StateMeta) -> None:
        """Persist and stream authoritative item outcomes after an action."""
        items, counts = self._surface_details(meta)
        async with self._sessions() as session:
            row = await session.get(db.StateNode, meta.id)
            if row is None:
                return
            row.interactables = items
            row.exploration = {
                **dict(row.exploration or {}),
                **counts,
                "visit_status": (
                    "fully_explored" if counts["pending"] == 0 else "partially_explored"
                ),
            }
            await session.commit()
        self._stats["surface_pending_items"] = sum(
            sum(
                self._final_status(state, item) == "pending"
                for item in state.interactables
            )
            for state in self._states.values()
        )
        self._emit(
            ExplorerEvent(
                EventType.SURFACE_ITEMS_DISCOVERED,
                f"Updated interactions for s{meta.index}",
                {
                    "state_id": meta.id,
                    "surface_items": self._surface_summary(meta),
                    "exploration": counts,
                    "counters": self._counters(),
                },
            )
        )

    async def _flush_state_details(self) -> None:
        """Write final surface-item statuses + per-state coverage at run end.

        State rows are written once at discovery time (statuses unknown then);
        this back-fills the exploration outcome so the UI and context pack can
        show explored/blocked/skipped/pending items and unexplored frontier.
        """
        pending_actions = 0
        pending_states = 0
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(db.StateNode).where(db.StateNode.run_id == self._run_id)
                    )
                )
                .scalars()
                .all()
            )
            rows_by_id = {row.id: row for row in rows}
            for meta in self._states.values():
                items, counts = self._surface_details(meta)

                row = rows_by_id.get(meta.id)
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
                        and self._family_registry.candidates.get(item["pattern"])
                        and self._family_registry.candidates[item["pattern"]].status != "rejected"
                    ]
                    existing_exploration = dict(row.exploration or {})
                    row.parent_state_id = meta.parent_state_id
                    row.state_type = meta.state_type.value
                    row.interactables = items
                    row.detected_flags = {
                        **dict(row.detected_flags or {}),
                        "page_role": meta.page_role.value,
                    }
                    row.exploration = {
                        **existing_exploration,
                        **counts,
                        "page_role": meta.page_role.value,
                        "page_anchor_id": meta.parent_state_id or meta.id,
                        "variant_kind": (
                            "page"
                            if meta.parent_state_id is None
                            else "page_variant"
                            if meta.state_type in {StateType.PAGE_VARIANT, StateType.TAB}
                            else "overlay"
                        ),
                        **(
                            {"inherited_surface_state_id": meta.parent_state_id}
                            if meta.parent_state_id is not None
                            else {}
                        ),
                        "nav_capabilities": nav_capabilities,
                        "surface_families": surface_families,
                        **(
                            {
                                "route_family": meta.route_family,
                                "family_variant_key": self._stable_hash(
                                    *(
                                        str(part)
                                        for part in (meta.family_template_key or ())
                                    ),
                                    length=20,
                                ),
                                "family_representative_state_id": (
                                    self._family_variants.get(meta.route_family, {}).get(
                                        meta.family_template_key
                                    )
                                    if meta.family_template_key is not None
                                    else meta.id
                                ),
                                "family_sampled": len(
                                    self._family_sampled_urls.get(meta.route_family, [])
                                ),
                                "family_skipped": self._family_skipped.get(meta.route_family, 0),
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
        self._sync_family_stats()
        flush_started = time.perf_counter()
        with contextlib.suppress(Exception):
            await self._flush_state_details()
        self._record_timing("finalize_seconds", flush_started)
        self._stats["actions_performed"] = self._budget.actions
        self._stats["duration_seconds"] = round(self._budget.elapsed_seconds, 2)
        self._stats["active_duration_seconds"] = round(
            self._budget.active_elapsed_seconds, 2
        )
        self._stats["auth_paused_duration_seconds"] = round(
            self._budget.auth_paused_seconds, 2
        )
        self._stats["frontier_actions"] = len(self._frontier)
        unresolved_required, failed_required = self._required_obligation_summary()
        pending_representatives = sum(
            candidate.status == "provisional"
            and len(set(candidate.sample_targets) - set(candidate.samples))
            for candidate in self._family_registry.candidates.values()
        ) + len(self._auth_deferred_family_actions)
        self._stats["pending_representative_actions"] = pending_representatives
        self._stats["unresolved_discovery_obligations"] = (
            unresolved_required + pending_representatives
        )
        self._stats["required_action_failures"] = failed_required
        stop_reason = self._stats.get("stop_reason")
        if status == "failed":
            completion_status = "failed"
        elif (
            stop_reason == "frontier_exhausted"
            and not self._stats.get("pending_actions")
            and not self._stats["unresolved_discovery_obligations"]
            and not failed_required
        ):
            completion_status = "complete"
        elif stop_reason == "frontier_exhausted":
            completion_status = "partial"
        else:
            completion_status = "budget_limited"
        self._stats["completion_status"] = completion_status
        self._stats["timings"] = {
            key: round(value, 3) for key, value in sorted(self._timings.items())
        }
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
