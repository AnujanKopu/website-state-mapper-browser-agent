"""Depth-first exploration: the core state-mapping loop.

DFS over actions: finish ranked affordances from the current branch before
backtracking. Within each state, higher-scored actions are tried first.
Each step navigates to its source state (replaying the stored path when
needed), performs the action, observes the result, and either merges into a
known state (identity or URL dedup) or registers a new node and enqueues its
own ranked, safety-filtered actions on top of the stack. Stops when the
stack drains or any budget is exhausted.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine import identity
from engine.browser.actions import click_interactable, click_selector, dismiss_cookie_banner
from engine.browser.autofill import autofill_auth_form
from engine.browser.session import BrowserSession
from engine.browser.snapshot import stabilize
from engine.capture import build_state_row, new_id, observe_page, persist_state
from engine.classify import StateAnalysis, analyze_state
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.events import ActionOutcome, EventType
from engine.identity import normalize_url
from engine.ranking import ActionCandidate, is_auth_entry, score_action
from engine.safety import is_same_origin
from engine.schemas import (
    ActionStep,
    BudgetConfig,
    Credentials,
    Interactable,
    Observation,
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


@dataclass
class PendingAction:
    from_state: StateMeta
    candidate: ActionCandidate


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
    """LIFO stack of pending actions for depth-first exploration."""

    def __init__(self) -> None:
        self._stack: list[PendingAction] = []

    def push(self, item: PendingAction) -> None:
        self._stack.append(item)

    def pop(self) -> PendingAction:
        return self._stack.pop()

    def __len__(self) -> int:
        return len(self._stack)


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

    async def run(self, url: str, *, run_id: str | None = None) -> str:
        """Explore `url` until budgets exhaust; returns the run id.

        A `run_id` may be supplied so a caller (e.g. the API) can register
        and return the id before exploration starts.
        """
        self._run_id = run_id or new_id()
        self._root_url = url
        self._budget = Budget(self._config.budgets)
        self._frontier = Frontier()
        self._identity = identity.IdentityIndex()
        self._states: dict[str, StateMeta] = {}
        self._visited_urls: set[str] = set()
        # First state registered for each normalized URL (inferred-edge target).
        self._url_to_state: dict[str, str] = {}
        # Structure-confirmed representatives and bounded sampling per
        # content-like route family inferred from repeated link cohorts.
        self._family_variants: dict[str, dict[tuple, str]] = {}
        self._family_attempts: dict[str, int] = {}
        self._family_sampled_urls: dict[str, list[str]] = {}
        self._family_skipped: dict[str, int] = {}
        self._edges_done: set[tuple[str, str]] = set()
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
                    },
                    "counters": self._counters(),
                },
            )
        )
        await page.goto(url)
        await dismiss_cookie_banner(page)

        observation = await observe_page(page, self._config)
        root = await self._register_state(
            observation, depth=0, path=[ActionStep(kind="goto", url=page.url)]
        )
        self._current_state_id = root.id

        if root.state_type == StateType.AUTH_WALL:
            post_auth = await self._handle_auth_wall(page, root)
            if post_auth is not None:
                await self._add_user_auth_edge(root, post_auth)

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

    def _inferred_target(self, source: StateMeta, item: Interactable) -> str | None:
        """If `item` is a plain same-origin link to an already-known URL state,
        return that state's id so we can record the edge without clicking."""
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
        target_id = self._url_to_state.get(target_norm)
        if target_id is None or target_id == source.id:
            return None
        return target_id

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
            self._stats["inferred_edges"] += 1
            self._emit_action_finished(
                ActionOutcome.DEDUPED,
                f"Inferred '{item.label}' -> known s{self._states[inferred_id].index}",
                from_state_id=source.id,
                to_state_id=inferred_id,
            )
            return

        family_pattern = pending.candidate.family_pattern
        if family_pattern is not None:
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
        observation = await observe_page(page, self._config)
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
            )
            self._actions_since_new = 0
            self._emit_action_finished(
                ActionOutcome.NEW_STATE,
                f"'{item.label}' reached new state s{destination.index}",
                from_state_id=source.id,
                to_state_id=destination.id,
            )

        await self._add_edge(source, destination, pending.candidate, via=via)
        self._edges_done.add(edge_key)
        self._item_outcome[(source.id, item.item_id)] = "explored"
        # A family merge leaves the browser on an alternate instance URL.
        # Force replay before expanding the representative's actions.
        self._current_state_id = (
            destination.id
            if observation.url_normalized == destination.url_normalized
            else None
        )

        # Auth-wall gate: pause exploration if the destination is a login page.
        # This runs after recording the edge so the gate node is always visible.
        if destination.state_type == StateType.AUTH_WALL:
            post_auth = await self._handle_auth_wall(page, destination)
            if post_auth is not None:
                await self._add_user_auth_edge(destination, post_auth)
                self._current_state_id = post_auth.id

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
        if observation.url_normalized == source.url_normalized:
            return None
        if observation.snapshot.signals.modal_open:
            return None
        return self._url_to_state.get(observation.url_normalized)

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
            if trigger.tag == "summary" or trigger.kind in ("disclosure", "menuitem"):
                return StateType.DROPDOWN
        return StateType.TAB

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
    ) -> StateMeta:
        analysis = analyze_state(observation, base_url=self._root_url)
        state_type = analysis.state_type
        if not is_same_origin(observation.snapshot.url, self._root_url):
            state_type = StateType.EXTERNAL  # reached via script redirect; never expanded

        # Same-URL sub-states (modal/tab/dropdown) hang off the page that
        # opened them; refine the type for non-modal structural changes.
        parent_state_id: str | None = None
        if source is not None and observation.url_normalized == source.url_normalized:
            parent_state_id = self._page_ancestor(source)
            if state_type == StateType.PAGE:
                state_type = self._substate_type(trigger)

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
        state.detected_flags = analysis.flags

        async with self._sessions() as session:
            row = build_state_row(state)
            row.parent_state_id = parent_state_id
            if route_family is not None:
                # Persist grouping metadata at discovery time so another tab
                # can hydrate the live family before run-finalization.
                row.exploration = {"route_family": route_family}
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
        )
        self._states[meta.id] = meta
        self._identity.add(key or identity.key_for(observation), meta.id)
        self._visited_urls.add(observation.url_normalized)
        self._url_to_state.setdefault(observation.url_normalized, meta.id)
        if parent_state_id is None and route_family is not None:
            self._family_variants.setdefault(route_family, {}).setdefault(
                self._family_template_key(observation), meta.id
            )
        self._stats["states"] += 1
        self._stats["actions_denied"] += len(analysis.denied)

        title = observation.snapshot.title or observation.url_normalized
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
                    "flags": analysis.flags,
                    "route_family": route_family,
                    "denied_count": len(analysis.denied),
                    "surface_items": self._surface_summary(meta),
                    "counters": self._counters(),
                },
            )
        )

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
        if observation.url_normalized != source.url_normalized:
            return [ActionStep(kind="goto", url=observation.snapshot.url)]
        return [*source.path, ActionStep(kind="click", selector=selector, label=label)]

    def _enqueue_actions(self, meta: StateMeta, analysis: StateAnalysis) -> None:
        if meta.depth >= self._config.budgets.max_depth:
            return
        if meta.state_type in _TERMINAL_TYPES:
            return
        if meta.state_type in _GATE_TYPES:
            return  # expanded only after auth_gate resolution via _handle_auth_wall
        eligible: list[ActionCandidate] = []
        reserved_by_family: dict[str, int] = {}
        for candidate in analysis.safe:
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
        # Top-K by score; push worst-first so stack pop tries the best action first.
        for candidate in reversed(top):
            self._frontier.push(PendingAction(from_state=meta, candidate=candidate))

    # ------------------------------------------------------------------
    # Auth gate
    # ------------------------------------------------------------------

    async def _handle_auth_wall(
        self, page: Page, meta: StateMeta
    ) -> StateMeta | None:
        """Handle reaching an AUTH_WALL state.

        1. Try autofill if credentials are available (Slice 6).
        2. Emit AUTH_GATE event.
        3. If a hook is registered, pause and await the user's decision.
        4. On resume: re-observe the page to capture the post-auth state.
        5. Return the post-auth StateMeta if auth succeeded, else None.
        """
        # --- Step 1: attempt credential autofill (Slice 6) ---
        if self._credentials is not None:
            try:
                submitted = await autofill_auth_form(
                    page,
                    self._credentials,
                    timeout_ms=self._config.exploration.action_timeout_ms,
                )
                if submitted:
                    await stabilize(page, self._config.browser.stabilize_quiet_ms)
                    observation = await observe_page(page, self._config)
                    key = identity.key_for(observation)
                    existing_id = self._resolve_existing_state(observation, meta, key)
                    # If the page changed and is no longer an auth wall, register it.
                    if (
                        existing_id is None
                        and observation.snapshot.signals.password_fields == 0
                    ):
                        post_auth = await self._register_state(
                            observation,
                            depth=meta.depth + 1,
                            path=[ActionStep(kind="goto", url=observation.snapshot.url)],
                            key=key,
                            source=meta,
                        )
                        self._actions_since_new = 0
                        self._emit(
                            ExplorerEvent(
                                EventType.AUTH_GATE,
                                f"Auth autofill succeeded; reached s{post_auth.index}",
                                {
                                    "state_id": meta.id,
                                    "url": meta.url,
                                    "title": observation.snapshot.title,
                                    "screenshot": meta.id,
                                    "decision": "autofilled",
                                    "post_auth_state_id": post_auth.id,
                                    "autofill_attempted": True,
                                },
                            )
                        )
                        return post_auth
                    # Autofill was submitted but page didn't change (wrong creds / captcha);
                    # fall through to emit auth_gate and pause.
            except Exception:  # noqa: BLE001 – autofill is best-effort
                pass

        # --- Step 2: emit AUTH_GATE event ---
        self._emit(
            ExplorerEvent(
                EventType.AUTH_GATE,
                f"Auth wall reached at {meta.url!r} — awaiting user decision",
                {
                    "state_id": meta.id,
                    "url": meta.url,
                    "title": meta.url,
                    "screenshot": meta.id,
                    "decision": None,
                    "autofill_attempted": self._credentials is not None,
                    "suggested_actions": ["resume", "skip"],
                },
            )
        )

        # --- Step 3: pause if a hook is wired (API-driven runs) ---
        if self._auth_gate_hook is None:
            return None  # non-API runs: skip the gate, continue exploration

        decision, new_credentials = await self._auth_gate_hook(meta.id, meta.url)

        if new_credentials is not None:
            self._credentials = new_credentials

        if decision == "skip":
            # Mark gate as skipped in the state's flags (best-effort DB update).
            with contextlib.suppress(Exception):
                async with self._sessions() as session:
                    row = await session.get(db.StateNode, meta.id)
                    if row is not None:
                        flags = dict(row.detected_flags or {})
                        flags["auth_gate_skipped"] = True
                        row.detected_flags = flags
                        await session.commit()
            return None

        # --- Step 4: decision == "resume" — try autofill with new/existing creds ---
        if self._credentials is not None:
            try:
                submitted = await autofill_auth_form(
                    page,
                    self._credentials,
                    timeout_ms=self._config.exploration.action_timeout_ms,
                )
                if submitted:
                    await stabilize(page, self._config.browser.stabilize_quiet_ms)
            except Exception:  # noqa: BLE001
                pass

        # Re-observe page after resume (user may have authenticated manually
        # in a headed browser, or autofill was just attempted above).
        try:
            await stabilize(page, self._config.browser.stabilize_quiet_ms)
            observation = await observe_page(page, self._config)
        except Exception:  # noqa: BLE001
            return None

        key = identity.key_for(observation)
        existing_id = self._resolve_existing_state(observation, meta, key)

        if existing_id is not None and existing_id != meta.id:
            # Arrived at an already-known state after auth.
            return self._states[existing_id]

        if observation.snapshot.signals.password_fields > 0:
            # Still on auth wall — auth didn't work, give up.
            return None

        # New post-auth state.
        post_auth = await self._register_state(
            observation,
            depth=meta.depth + 1,
            path=[ActionStep(kind="goto", url=observation.snapshot.url)],
            key=key,
            source=meta,
        )
        self._actions_since_new = 0
        return post_auth

    async def _add_user_auth_edge(
        self, source: StateMeta, destination: StateMeta
    ) -> None:
        """Record a user-authenticated transition (manual or credential autofill)."""
        edge_id = uuid.uuid4().hex
        async with self._sessions() as session:
            session.add(
                db.Edge(
                    id=edge_id,
                    run_id=self._run_id,
                    from_state_id=source.id,
                    to_state_id=destination.id,
                    action_type="user_auth",
                    label="User authenticated",
                    selector="",
                    selector_strategy="css",
                    confidence=1.0,
                    via="user_auth",
                )
            )
            await session.commit()

        self._stats["edges"] += 1
        self._emit(
            ExplorerEvent(
                EventType.EDGE_DISCOVERED,
                f"s{source.index} -> s{destination.index}: User authenticated",
                {
                    "edge_id": edge_id,
                    "from": source.id,
                    "to": destination.id,
                    "from_index": source.index,
                    "to_index": destination.index,
                    "action": "user_auth",
                    "label": "User authenticated",
                    "selector": "",
                    "via": "user_auth",
                    "surface_item_id": None,
                    "counters": self._counters(),
                },
            )
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

    async def _add_edge(
        self,
        source: StateMeta,
        destination: StateMeta,
        candidate: ActionCandidate,
        *,
        via: str = "performed",
    ) -> None:
        item = candidate.interactable
        label = f"Links to '{item.label}'" if via == "inferred" else f"Clicked '{item.label}'"
        if candidate.collapsed_count > 1:
            label += f" (1 of {candidate.collapsed_count} similar)"

        edge_id = uuid.uuid4().hex
        async with self._sessions() as session:
            session.add(
                db.Edge(
                    id=edge_id,
                    run_id=self._run_id,
                    from_state_id=source.id,
                    to_state_id=destination.id,
                    action_type="click",
                    label=label,
                    selector=item.selector,
                    selector_strategy="css",
                    element_text=item.text,
                    confidence=max(0.0, min(1.0, candidate.score / 100.0)),
                    collapsed_count=candidate.collapsed_count,
                    via=via,
                    surface_item_id=item.item_id or None,
                )
            )
            await session.commit()

        self._stats["edges"] += 1
        self._emit(
            ExplorerEvent(
                EventType.EDGE_DISCOVERED,
                f"s{source.index} -> s{destination.index}: {label}",
                {
                    "edge_id": edge_id,
                    "from": source.id,
                    "to": destination.id,
                    "from_index": source.index,
                    "to_index": destination.index,
                    "action": "click",
                    "label": label,
                    "selector": item.selector,
                    "via": via,
                    "surface_item_id": item.item_id or None,
                    "counters": self._counters(),
                },
            )
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
                    row.interactables = items
                    row.exploration = {
                        **counts,
                        **(
                            {
                                "route_family": meta.route_family,
                                "family_sampled": len(
                                    self._family_sampled_urls.get(meta.route_family, [])
                                ),
                                "family_skipped": self._family_skipped.get(
                                    meta.route_family, 0
                                ),
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

    async def _finish_run(self, status: str, *, error: str | None = None) -> None:
        with contextlib.suppress(Exception):
            await self._flush_state_details()
        self._stats["actions_performed"] = self._budget.actions
        self._stats["duration_seconds"] = round(self._budget.elapsed_seconds, 2)
        stop_reason = self._stats.get("stop_reason")
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
                    {"error": error, "stop_reason": stop_reason, "stats": dict(self._stats)},
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
                    "stats": dict(self._stats),
                    "counters": self._counters(),
                },
            )
        )
