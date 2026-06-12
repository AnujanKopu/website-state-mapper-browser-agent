"""Run lifecycle and live-event fan-out for the API.

The explorer is a synchronous-callback emitter that runs inside the asyncio
event loop. `RunManager` launches each exploration as a background task and
bridges its `ExplorerEvent` callbacks into per-run pub/sub: every event is
wrapped in the SSE transport envelope (contract v1), appended to a replay
buffer, and pushed to all live SSE subscribers.

Because the explorer's `on_event` callback and the SSE consumers share one
event loop, the bridge uses non-blocking `Queue.put_nowait`, and subscriber
registration captures the replay buffer with no intervening `await` -- so a
late subscriber sees every event exactly once, with none lost or duplicated.

A per-run heartbeat task emits `heartbeat` events on a fixed interval so the
UI can tell a live-but-quiet run from a dead stream; terminal events
(`run_completed` / `run_failed`) close the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from engine.capture import new_id
from engine.config import Settings
from engine.events import TERMINAL_EVENTS, EventType
from engine.explorer import Explorer, ExplorerEvent
from engine.schemas import Credentials, RunConfig, RunStatus

# Pushed onto subscriber queues to signal end-of-stream.
_SENTINEL = object()

# Seconds between heartbeat events while a run is live.
HEARTBEAT_INTERVAL_SECONDS = 10


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class StreamedEvent:
    """The SSE transport envelope (contract v1)."""

    event_id: str
    run_id: str
    sequence: int
    timestamp: str
    type: str
    payload: dict

    def to_envelope(self) -> dict:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
        }


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
    heartbeat_task: asyncio.Task | None = None
    done: bool = field(default_factory=bool)
    terminal_emitted: bool = field(default_factory=bool)
    latest_counters: dict = field(default_factory=dict)
    _seq: int = 0

    # Auth gate state (Slices 5-6)
    credentials: Credentials | None = None
    auth_gate: dict | None = None  # set while paused at an auth wall
    _auth_gate_event: asyncio.Event | None = field(default=None, repr=False)
    _auth_gate_decision: str = field(default="skip", repr=False)
    _auth_gate_creds: Credentials | None = field(default=None, repr=False)

    def _envelope(self, event_type: str, payload: dict) -> StreamedEvent:
        event = StreamedEvent(
            event_id=uuid.uuid4().hex,
            run_id=self.run_id,
            sequence=self._seq,
            timestamp=_now_iso(),
            type=event_type,
            payload=payload,
        )
        self._seq += 1
        self.history.append(event)
        return event

    def _fan_out(self, event: StreamedEvent) -> None:
        for queue in self.subscribers:
            queue.put_nowait(event)

    def publish(self, event: ExplorerEvent) -> None:
        """Wrap an explorer event in an envelope and fan it out."""
        payload = {"message": event.message, **event.data}
        if "counters" in event.data:
            self.latest_counters = event.data["counters"]
        event_type = str(event.kind)
        if event_type in TERMINAL_EVENTS:
            self.terminal_emitted = True
        if event_type == EventType.AUTH_GATE and event.data.get("decision") is None:
            # Explorer is pausing at an auth wall; reflect this in the handle status.
            self.status = RunStatus.PAUSED
        self._fan_out(self._envelope(event_type, payload))

    # ------------------------------------------------------------------
    # Auth gate (Slices 5-6)
    # ------------------------------------------------------------------

    def set_auth_gate(self, state_id: str, url: str) -> None:
        """Called by the auth_gate_hook when the explorer pauses."""
        self.auth_gate = {"state_id": state_id, "url": url}
        self._auth_gate_event = asyncio.Event()
        self._auth_gate_decision = "skip"
        self._auth_gate_creds = None

    async def wait_auth_decision(self) -> tuple[str, Credentials | None]:
        """Suspend until resume/skip is called via the API."""
        assert self._auth_gate_event is not None, "set_auth_gate must be called first"
        await self._auth_gate_event.wait()
        return self._auth_gate_decision, self._auth_gate_creds

    def resolve_auth_gate(
        self, decision: str, credentials: Credentials | None = None
    ) -> bool:
        """API-callable: unblock the explorer. Returns False if no gate is pending."""
        if not self.auth_gate:
            return False
        self._auth_gate_decision = decision
        self._auth_gate_creds = credentials
        self.auth_gate = None
        self.status = RunStatus.RUNNING
        assert self._auth_gate_event is not None
        self._auth_gate_event.set()
        return True

    def publish_heartbeat(self) -> None:
        envelope = self._envelope(
            EventType.HEARTBEAT.value,
            {
                "message": "",
                "counters": self.latest_counters,
                "frontier_size": self.latest_counters.get("frontier_size", 0),
            },
        )
        self._fan_out(envelope)

    def complete(self, status: RunStatus, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.done = True
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
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

    def start_run(
        self,
        url: str,
        *,
        overrides: dict | None = None,
        credentials: Credentials | None = None,
    ) -> RunHandle:
        """Register a run and launch it as a background task; returns at once."""
        run_id = new_id()
        handle = RunHandle(run_id=run_id, url=url, status=RunStatus.RUNNING)
        handle.credentials = credentials
        self._handles[run_id] = handle

        config = self._config_with_overrides(overrides or {})

        def auth_gate_hook(state_id: str, url_: str):
            handle.set_auth_gate(state_id, url_)
            return handle.wait_auth_decision()

        explorer = Explorer(
            self._settings,
            config,
            on_event=handle.publish,
            auth_gate_hook=auth_gate_hook,
            credentials=credentials,
        )
        handle.task = asyncio.create_task(
            self._drive(explorer, handle), name=f"explore:{run_id}"
        )
        handle.heartbeat_task = asyncio.create_task(
            self._heartbeat(handle), name=f"heartbeat:{run_id}"
        )
        return handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    async def shutdown(self) -> None:
        """Cancel any in-flight runs and heartbeats (called on app shutdown)."""
        tasks: list[asyncio.Task] = []
        for handle in self._handles.values():
            for task in (handle.task, handle.heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
                    tasks.append(task)
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

    async def _heartbeat(self, handle: RunHandle) -> None:
        """Emit periodic heartbeats until the run finishes."""
        try:
            while not handle.done:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if handle.done:
                    return
                handle.publish_heartbeat()
        except asyncio.CancelledError:
            return

    async def _drive(self, explorer: Explorer, handle: RunHandle) -> None:
        try:
            await explorer.run(handle.url, run_id=handle.run_id)
            handle.complete(RunStatus.DONE)
        except asyncio.CancelledError:
            handle.complete(RunStatus.CANCELLED, error="run cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as run state
            # The explorer emits `run_failed` itself once exploration has begun;
            # this covers failures before that point (e.g. browser launch).
            if not handle.terminal_emitted:
                handle.publish(ExplorerEvent(EventType.RUN_FAILED, str(exc), {"error": str(exc)}))
            handle.complete(RunStatus.FAILED, error=str(exc))

    def _config_with_overrides(self, overrides: dict) -> RunConfig:
        config = self._run_config.model_copy(deep=True)
        if (headless := overrides.get("headless")) is not None:
            config.browser.headless = headless
        if (save_dom := overrides.get("save_dom_snapshots")) is not None:
            config.capture.save_dom_snapshots = save_dom
        budget_fields = ("max_states", "max_actions", "max_depth", "max_wall_seconds")
        for name in budget_fields:
            if (value := overrides.get(name)) is not None:
                setattr(config.budgets, name, value)
        return config
