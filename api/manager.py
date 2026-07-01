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
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.supervisor_client import SupervisorClient
from engine.capture import new_id
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory
from engine.events import TERMINAL_EVENTS, EventType
from engine.explorer import Explorer, ExplorerEvent
from engine.network_policy import validate_public_http_url
from engine.schemas import Credentials, RunConfig, RunStatus
from engine.storage import StorageBackend

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
    _control_sink: Callable[[str, Credentials | None], None] | None = field(
        default=None, repr=False
    )

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

    @property
    def last_sequence(self) -> int:
        """Highest sequence already published, or -1 before the first event."""
        return self._seq - 1

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
            if self.heartbeat_task is not None:
                self.heartbeat_task.cancel()
        if event_type == EventType.AUTH_GATE and event.data.get("decision") is None:
            # Explorer is pausing at an auth wall; reflect this in the handle status.
            self.status = RunStatus.PAUSED
            if (
                self._control_sink is not None
                and self.auth_gate is None
                and event.data.get("state_id")
            ):
                self.set_auth_gate(
                    str(event.data["state_id"]), str(event.data.get("url") or self.url)
                )
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
        resolved_gate = dict(self.auth_gate)
        self._auth_gate_decision = decision
        self._auth_gate_creds = credentials
        self.auth_gate = None
        self.status = RunStatus.RUNNING
        # Publish an authoritative resolution after the pending gate. Every
        # connected tab receives it, and late subscribers replay both events
        # in order instead of resurrecting a stale auth prompt.
        self._fan_out(
            self._envelope(
                EventType.AUTH_GATE.value,
                {
                    "message": f"Authentication gate {decision}",
                    "state_id": resolved_gate["state_id"],
                    "url": resolved_gate["url"],
                    "title": resolved_gate["url"],
                    "screenshot": "",
                    "decision": decision,
                    "autofill_attempted": credentials is not None,
                    "suggested_actions": [],
                },
            )
        )
        assert self._auth_gate_event is not None
        if self._control_sink is not None:
            self._control_sink(decision, credentials)
        else:
            self._auth_gate_event.set()
        return True

    def publish_heartbeat(self) -> None:
        if self.terminal_emitted or self.done:
            return
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

    def __init__(
        self,
        settings: Settings,
        run_config: RunConfig,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        store: StorageBackend | None = None,
    ) -> None:
        self._settings = settings
        self._run_config = run_config
        self._session_factory = session_factory
        self._store = store
        self._handles: dict[str, RunHandle] = {}
        self._supervisor = (
            SupervisorClient(settings.supervisor_url, settings.supervisor_token)
            if settings.hosted_mode and settings.supervisor_url
            else None
        )

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
        if self._settings.hosted_mode:
            validate_public_http_url(url)
            if self._supervisor is None:
                raise ValueError("hosted mode requires FLOWSTATE_SUPERVISOR_URL")
        run_id = new_id()
        handle = RunHandle(run_id=run_id, url=url, status=RunStatus.RUNNING)
        handle.credentials = credentials
        self._handles[run_id] = handle

        config = self._config_with_overrides(overrides or {})

        if self._settings.hosted_mode:
            assert self._supervisor is not None

            def send_control(decision: str, creds: Credentials | None) -> None:
                command = "auth_resume" if decision == "resume" else "auth_skip"
                asyncio.create_task(self._supervisor.command(run_id, command, creds))

            handle._control_sink = send_control
            handle.task = asyncio.create_task(
                self._drive_worker(handle, config), name=f"worker:{run_id}"
            )
        else:
            def auth_gate_hook(state_id: str, url_: str):
                handle.set_auth_gate(state_id, url_)
                return handle.wait_auth_decision()

            explorer = Explorer(
                self._settings,
                config,
                on_event=handle.publish,
                auth_gate_hook=auth_gate_hook,
                credentials=credentials,
                session_factory=self._session_factory,
                store=self._store,
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
            if (
                self._supervisor is not None
                and handle.task is not None
                and not handle.task.done()
            ):
                with contextlib.suppress(Exception):
                    await self._supervisor.command(handle.run_id, "cancel")
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

    async def subscribe(self, handle: RunHandle, *, after_sequence: int = -1):
        """Yield every event for a run: buffered history first, then live."""
        queue, history, already_done = self._attach(
            handle, after_sequence=after_sequence
        )
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

    def _attach(
        self, handle: RunHandle, *, after_sequence: int = -1
    ) -> tuple[asyncio.Queue, list[StreamedEvent], bool]:
        """Atomically snapshot history and register a subscriber.

        Contains no `await`, so `publish` cannot interleave: events already in
        `history` are returned for replay, and any later event is delivered via
        the queue -- guaranteeing exactly-once delivery across the handoff.
        """
        queue: asyncio.Queue = asyncio.Queue()
        history = [event for event in handle.history if event.sequence > after_sequence]
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

    async def _drive_worker(self, handle: RunHandle, config: RunConfig) -> None:
        """Stream one disposable worker, hydrating committed state before SSE."""
        assert self._supervisor is not None
        try:
            async for message in self._supervisor.stream_run(
                run_id=handle.run_id,
                url=handle.url,
                config=config,
                credentials=handle.credentials,
            ):
                message_type = message.get("type")
                if message_type == "artifact_manifest":
                    continue
                if message_type == "worker_error":
                    raise RuntimeError(str(message.get("error") or "worker failed"))
                # The worker commits before writing each envelope. Mirror that
                # snapshot into API storage before publishing the event.
                await self._import_worker_snapshot(handle.run_id)
                payload = dict(message.get("payload") or {})
                event_message = str(payload.pop("message", ""))
                handle.publish(
                    ExplorerEvent(
                        EventType(str(message_type)), event_message, payload
                    )
                )
            await self._import_worker_snapshot(handle.run_id)
            handle.complete(RunStatus.DONE)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._supervisor.command(handle.run_id, "cancel")
            handle.complete(RunStatus.CANCELLED, error="run cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - worker boundary
            if not handle.terminal_emitted:
                handle.publish(
                    ExplorerEvent(EventType.RUN_FAILED, str(exc), {"error": str(exc)})
                )
            handle.complete(RunStatus.FAILED, error=str(exc))
        finally:
            with contextlib.suppress(Exception):
                await self._supervisor.cleanup(handle.run_id)

    async def _import_worker_snapshot(self, run_id: str) -> None:
        """Merge one worker's per-run SQLite graph into the API database."""
        if self._session_factory is None:
            raise RuntimeError("hosted worker import requires an API session factory")
        job_dir = (self._settings.worker_job_root / run_id).resolve()
        root = self._settings.worker_job_root.resolve()
        if root not in job_dir.parents:
            raise RuntimeError("worker job escaped configured root")
        database = job_dir / "flowstate.db"
        if not database.exists():
            return

        source_engine = create_db_engine(
            f"sqlite+aiosqlite:///{database.as_posix()}"
        )
        source_sessions = create_session_factory(source_engine)
        records: list[tuple[type, dict]] = []
        try:
            async with source_sessions() as source:
                run = await source.get(db.Run, run_id)
                if run is None:
                    return
                records.append((db.Run, self._row_data(run)))
                for model in (db.StateNode, db.Edge):
                    rows = (
                        await source.execute(
                            select(model).where(model.run_id == run_id)
                        )
                    ).scalars()
                    records.extend((model, self._row_data(row)) for row in rows)
        finally:
            await source_engine.dispose()

        async with self._session_factory() as destination:
            for model, values in records:
                await destination.merge(model(**values))
            await destination.commit()
        self._copy_worker_screenshots(job_dir, run_id)

    @staticmethod
    def _row_data(row: object) -> dict:
        return {
            column.name: getattr(row, column.name)
            for column in row.__table__.columns  # type: ignore[attr-defined]
        }

    def _copy_worker_screenshots(self, job_dir: Path, run_id: str) -> None:
        source = job_dir / "artifacts" / "runs" / run_id / "screenshots"
        if not source.exists():
            return
        target = self._settings.data_dir / "runs" / run_id / "screenshots"
        target.mkdir(parents=True, exist_ok=True)
        expected_formats = {".png": "PNG", ".webp": "WEBP"}
        for image in source.iterdir():
            expected_format = expected_formats.get(image.suffix.lower())
            if expected_format is None or not image.is_file():
                continue
            if source.resolve() not in image.resolve().parents:
                continue
            try:
                with Image.open(image) as candidate:
                    if candidate.format != expected_format:
                        continue
                    candidate.verify()
            except (OSError, UnidentifiedImageError):
                continue
            shutil.copy2(image, target / image.name)

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
        if (auth_mode := overrides.get("auth_mode")) is not None:
            config.authentication.mode = auth_mode
        return config
