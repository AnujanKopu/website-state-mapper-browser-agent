"""Run lifecycle and live-event fan-out for the API.

The explorer is a synchronous-callback emitter that runs inside the asyncio
event loop. `RunManager` launches each exploration as a background task and
bridges its `ExplorerEvent` callbacks into per-run pub/sub: every event is
appended to a replay buffer and pushed to all live SSE subscribers.

Because the explorer's `on_event` callback and the SSE consumers share one
event loop, the bridge uses non-blocking `Queue.put_nowait`, and subscriber
registration captures the replay buffer with no intervening `await` -- so a
late subscriber sees every event exactly once, with none lost or duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from engine.capture import new_id
from engine.config import Settings
from engine.explorer import Explorer, ExplorerEvent
from engine.schemas import RunConfig, RunStatus

# Pushed onto subscriber queues to signal end-of-stream.
_SENTINEL = object()


@dataclass
class StreamedEvent:
    """An explorer event tagged with a monotonic per-run sequence number."""

    seq: int
    kind: str
    message: str
    data: dict


@dataclass
class RunHandle:
    """Live state for one run: status plus its event replay buffer."""

    run_id: str
    url: str
    status: RunStatus = RunStatus.QUEUED
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    history: list[StreamedEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    done: bool = field(default_factory=bool)

    def publish(self, event: ExplorerEvent) -> None:
        """Record an explorer event and fan it out to live subscribers."""
        streamed = StreamedEvent(
            seq=len(self.history), kind=event.kind, message=event.message, data=event.data
        )
        self.history.append(streamed)
        for queue in self.subscribers:
            queue.put_nowait(streamed)

    def complete(self, status: RunStatus, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.done = True
        for queue in self.subscribers:
            queue.put_nowait(_SENTINEL)


class RunManager:
    """Owns running explorations and their event streams."""

    def __init__(self, settings: Settings, run_config: RunConfig) -> None:
        self._settings = settings
        self._run_config = run_config
        self._handles: dict[str, RunHandle] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_run(self, url: str, *, overrides: dict | None = None) -> RunHandle:
        """Register a run and launch it as a background task; returns at once."""
        run_id = new_id()
        handle = RunHandle(run_id=run_id, url=url, status=RunStatus.RUNNING)
        self._handles[run_id] = handle

        config = self._config_with_overrides(overrides or {})
        explorer = Explorer(self._settings, config, on_event=handle.publish)
        handle.task = asyncio.create_task(
            self._drive(explorer, handle), name=f"explore:{run_id}"
        )
        return handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    async def shutdown(self) -> None:
        """Cancel any in-flight runs (called on app shutdown)."""
        tasks = [h.task for h in self._handles.values() if h.task and not h.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    async def subscribe(self, handle: RunHandle):
        """Yield every event for a run: buffered history first, then live."""
        queue, history, already_done = self._attach(handle)
        try:
            for event in history:
                yield event
            if already_done:
                return
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    return
                yield item
        finally:
            handle.subscribers.discard(queue)

    def _attach(self, handle: RunHandle) -> tuple[asyncio.Queue, list[StreamedEvent], bool]:
        """Atomically snapshot history and register a subscriber.

        Contains no `await`, so `publish` cannot interleave: events already in
        `history` are returned for replay, and any later event is delivered via
        the queue -- guaranteeing exactly-once delivery across the handoff.
        """
        queue: asyncio.Queue = asyncio.Queue()
        history = list(handle.history)
        if not handle.done:
            handle.subscribers.add(queue)
        return queue, history, handle.done

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _drive(self, explorer: Explorer, handle: RunHandle) -> None:
        try:
            await explorer.run(handle.url, run_id=handle.run_id)
            handle.complete(RunStatus.DONE)
        except asyncio.CancelledError:
            handle.complete(RunStatus.CANCELLED, error="run cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as run state
            handle.publish(ExplorerEvent("run_error", str(exc)))
            handle.complete(RunStatus.FAILED, error=str(exc))

    def _config_with_overrides(self, overrides: dict) -> RunConfig:
        config = self._run_config.model_copy(deep=True)
        if (headless := overrides.get("headless")) is not None:
            config.browser.headless = headless
        budget_fields = ("max_states", "max_actions", "max_depth", "max_wall_seconds")
        for name in budget_fields:
            if (value := overrides.get(name)) is not None:
                setattr(config.budgets, name, value)
        return config
