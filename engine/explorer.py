"""Frontier-based exploration: the core state-mapping loop.

Priority best-first search over actions. Each iteration pops the highest
value pending action, navigates to its source state (replaying the stored
path when needed), performs the action, observes the result, and either
merges into a known state (dedup) or registers a new node and enqueues its
own ranked, safety-filtered actions. Stops when the frontier drains or any
budget is exhausted.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from engine import identity
from engine.browser.actions import dismiss_cookie_banner
from engine.browser.session import BrowserSession
from engine.browser.snapshot import stabilize
from engine.capture import build_state_row, new_id, observe_page, persist_state
from engine.classify import StateAnalysis, analyze_state
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.ranking import ActionCandidate, score_action
from engine.safety import is_same_origin
from engine.schemas import ActionStep, BudgetConfig, Observation, RunConfig, StateType
from engine.storage import LocalStorage

# Stop when this many consecutive actions produce no new state (novelty collapse).
NOVELTY_PATIENCE = 30

_DEPTH_PENALTY = 5.0

_TERMINAL_TYPES = frozenset(
    {StateType.EXTERNAL, StateType.DEAD_END, StateType.RISKY_TERMINAL}
)


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
    """Max-priority queue of pending actions (heapq with negated priority)."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, PendingAction]] = []
        self._seq = itertools.count()

    def push(self, priority: float, item: PendingAction) -> None:
        heapq.heappush(self._heap, (-priority, next(self._seq), item))

    def pop(self) -> PendingAction:
        return heapq.heappop(self._heap)[2]

    def __len__(self) -> int:
        return len(self._heap)


class Explorer:
    """Runs one exploration and persists the resulting state graph."""

    def __init__(
        self,
        settings: Settings,
        config: RunConfig,
        *,
        on_event: EventSink | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._emit: EventSink = on_event or (lambda event: None)

    async def run(self, url: str) -> str:
        """Explore `url` until budgets exhaust; returns the run id."""
        self._run_id = new_id()
        self._root_url = url
        self._budget = Budget(self._config.budgets)
        self._frontier = Frontier()
        self._identity = identity.IdentityIndex()
        self._states: dict[str, StateMeta] = {}
        self._visited_urls: set[str] = set()
        self._edges_done: set[tuple[str, str]] = set()
        self._current_state_id: str | None = None
        self._actions_since_new = 0
        self._stats: dict = {
            "states": 0,
            "edges": 0,
            "dedup_hits": 0,
            "noop_actions": 0,
            "failed_actions": 0,
            "actions_denied": 0,
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
        self._emit(ExplorerEvent("run_started", f"Exploring {url}"))
        await page.goto(url)
        await dismiss_cookie_banner(page)

        observation = await observe_page(page, self._config)
        root = await self._register_state(
            observation, depth=0, path=[ActionStep(kind="goto", url=page.url)]
        )
        self._current_state_id = root.id

        while len(self._frontier):
            reason = self._budget.stop_reason(len(self._states), self._actions_since_new)
            if reason:
                self._stats["stop_reason"] = reason
                self._emit(ExplorerEvent("budget_exhausted", f"Stopping: {reason}"))
                break
            await self._execute(page, self._frontier.pop())
        else:
            self._stats["stop_reason"] = "frontier_exhausted"
            self._emit(ExplorerEvent("frontier_exhausted", "No actions left to explore"))

    async def _execute(self, page: Page, pending: PendingAction) -> None:
        source = pending.from_state
        item = pending.candidate.interactable
        edge_key = (source.id, item.selector)
        if edge_key in self._edges_done:
            return

        self._budget.note_action()
        self._actions_since_new += 1

        if not await self._ensure_at(page, source):
            self._stats["failed_actions"] += 1
            self._emit(
                ExplorerEvent("action_failed", f"Could not replay path to s{source.index}")
            )
            return

        try:
            await page.click(
                item.selector, timeout=self._config.exploration.action_timeout_ms
            )
        except PlaywrightError:
            self._current_state_id = None  # page state is now unknown
            self._stats["failed_actions"] += 1
            self._emit(ExplorerEvent("action_failed", f"Click failed on {item.label!r}"))
            return

        await self._absorb_popups(page)
        observation = await observe_page(page, self._config)
        key = identity.key_for(observation)
        existing_id = self._identity.find(key)

        if existing_id == source.id:
            # The action changed nothing meaningful; don't record a self-loop.
            self._stats["noop_actions"] += 1
            self._current_state_id = source.id
            self._emit(ExplorerEvent("noop", f"'{item.label}' changed nothing"))
            return

        if existing_id is not None:
            destination = self._states[existing_id]
            self._stats["dedup_hits"] += 1
            self._emit(
                ExplorerEvent(
                    "state_deduped",
                    f"'{item.label}' led to known state s{destination.index}",
                )
            )
        else:
            destination = await self._register_state(
                observation,
                depth=source.depth + 1,
                path=self._path_for(observation, source, item.selector, item.label),
                key=key,
            )
            self._actions_since_new = 0

        await self._add_edge(source, destination, pending.candidate)
        self._edges_done.add(edge_key)
        self._current_state_id = destination.id

    # ------------------------------------------------------------------
    # State registration
    # ------------------------------------------------------------------

    async def _register_state(
        self,
        observation: Observation,
        *,
        depth: int,
        path: list[ActionStep],
        key: identity.StateKey | None = None,
    ) -> StateMeta:
        analysis = analyze_state(observation, base_url=self._root_url)
        state_type = analysis.state_type
        if not is_same_origin(observation.snapshot.url, self._root_url):
            state_type = StateType.EXTERNAL  # reached via script redirect; never expanded

        state = persist_state(
            observation,
            run_id=self._run_id,
            state_id=new_id(),
            depth=depth,
            path=path,
            store=self._store,
        )
        state.state_type = state_type
        state.detected_flags = analysis.flags

        async with self._sessions() as session:
            session.add(build_state_row(state))
            await session.commit()

        meta = StateMeta(
            id=state.state_id,
            index=len(self._states),
            url=observation.snapshot.url,
            url_normalized=observation.url_normalized,
            depth=depth,
            path=path,
            state_type=state_type,
        )
        self._states[meta.id] = meta
        self._identity.add(key or identity.key_for(observation), meta.id)
        self._visited_urls.add(observation.url_normalized)
        self._stats["states"] += 1
        self._stats["actions_denied"] += len(analysis.denied)

        title = observation.snapshot.title or observation.url_normalized
        self._emit(
            ExplorerEvent(
                "state_new",
                f"s{meta.index} [{state_type.value}] {title!r} (depth {depth})",
                {"state_id": meta.id, "type": state_type.value},
            )
        )
        if analysis.denied:
            self._emit(
                ExplorerEvent(
                    "actions_blocked",
                    f"s{meta.index}: {len(analysis.denied)} risky action(s) blocked: "
                    + ", ".join(
                        f"'{c.interactable.label}' ({d.category.value})"
                        for c, d in analysis.denied[:5]
                    ),
                )
            )

        self._enqueue_actions(meta, analysis)
        return meta

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
        for candidate in analysis.safe:
            candidate.score = score_action(candidate, visited_urls=self._visited_urls)
        ranked = sorted(analysis.safe, key=lambda c: c.score, reverse=True)
        for candidate in ranked[: self._config.exploration.max_actions_per_state]:
            priority = candidate.score - _DEPTH_PENALTY * meta.depth
            self._frontier.push(priority, PendingAction(from_state=meta, candidate=candidate))

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
                    await page.click(
                        step.selector, timeout=self._config.exploration.action_timeout_ms
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
        self, source: StateMeta, destination: StateMeta, candidate: ActionCandidate
    ) -> None:
        item = candidate.interactable
        label = f"Clicked '{item.label}'"
        if candidate.collapsed_count > 1:
            label += f" (1 of {candidate.collapsed_count} similar)"

        async with self._sessions() as session:
            session.add(
                db.Edge(
                    id=uuid.uuid4().hex,
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
                )
            )
            await session.commit()

        self._stats["edges"] += 1
        self._emit(
            ExplorerEvent(
                "edge_created",
                f"s{source.index} -> s{destination.index}: {label}",
                {"from": source.id, "to": destination.id},
            )
        )

    async def _finish_run(self, status: str, *, error: str | None = None) -> None:
        self._stats["actions_performed"] = self._budget.actions
        self._stats["duration_seconds"] = round(self._budget.elapsed_seconds, 2)
        async with self._sessions() as session:
            run = await session.get(db.Run, self._run_id)
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.stats = self._stats
            if error is not None:
                run.error = error
            await session.commit()
        self._emit(
            ExplorerEvent(
                "run_finished",
                f"{status}: {self._stats['states']} states, {self._stats['edges']} edges, "
                f"{self._stats['actions_performed']} actions "
                f"in {self._stats['duration_seconds']}s",
                dict(self._stats),
            )
        )
