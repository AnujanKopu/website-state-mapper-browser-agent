"""Line-delimited command protocol for one isolated exploration run.

stdin commands: start, auth_resume, auth_skip, cancel
stdout messages: the existing SSE envelopes, followed by artifact_manifest
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from engine.config import Settings
from engine.explorer import Explorer, ExplorerEvent
from engine.network_policy import is_public_destination
from engine.schemas import Credentials, RunConfig


class WorkerProtocol:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.sequence = 0
        self.auth_commands: asyncio.Queue[tuple[str, Credentials | None]] = asyncio.Queue()

    def write(self, message: dict) -> None:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def publish(self, event: ExplorerEvent) -> None:
        self.write(
            {
                "event_id": uuid.uuid4().hex,
                "run_id": self.run_id,
                "sequence": self.sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "type": str(event.kind),
                "payload": {"message": event.message, **event.data},
            }
        )
        self.sequence += 1

    async def auth_gate(self, _state_id: str, _url: str) -> tuple[str, Credentials | None]:
        return await self.auth_commands.get()


async def _read_command() -> dict | None:
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return None
    return json.loads(line)


def _credentials(payload: dict | None) -> Credentials | None:
    if not payload:
        return None
    return Credentials(username=payload.get("username"), password=payload.get("password"))


async def run_worker() -> int:
    start = await _read_command()
    if not start or start.get("type") != "start":
        raise ValueError("first worker command must be 'start'")
    url = str(start.get("url") or "")
    if not await is_public_destination(url):
        raise ValueError("url did not resolve exclusively to public HTTP(S) addresses")

    run_id = str(start.get("run_id") or uuid.uuid4().hex)
    protocol = WorkerProtocol(run_id)
    config = RunConfig.model_validate(start.get("config") or {})
    config.browser.headless = True
    config.browser.allow_private_networks = False
    config.capture.save_dom_snapshots = bool(start.get("save_dom_snapshots", False))
    job_dir = Path("/job")
    settings = Settings(
        database_url="sqlite+aiosqlite:////job/flowstate.db",
        data_dir=job_dir / "artifacts",
        run_config_path=Path("/app/config/default_run.yaml"),
        hosted_mode=True,
    )
    explorer = Explorer(
        settings,
        config,
        on_event=protocol.publish,
        auth_gate_hook=protocol.auth_gate,
        credentials=_credentials(start.get("credentials")),
    )
    run_task = asyncio.create_task(explorer.run(url, run_id=run_id))

    while not run_task.done():
        command_task = asyncio.create_task(_read_command())
        done, _pending = await asyncio.wait(
            {run_task, command_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task in done:
            command_task.cancel()
            break
        command = command_task.result()
        if command is None:
            await run_task
            break
        kind = command.get("type")
        if kind == "auth_resume":
            await protocol.auth_commands.put(("resume", _credentials(command.get("credentials"))))
        elif kind == "auth_skip":
            await protocol.auth_commands.put(("skip", None))
        elif kind == "cancel":
            run_task.cancel()
            break

    with contextlib.suppress(asyncio.CancelledError):
        await run_task
    protocol.write(
        {
            "type": "artifact_manifest",
            "run_id": run_id,
            "database": "/job/flowstate.db",
            "artifacts": f"/job/artifacts/runs/{run_id}",
        }
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run_worker()))
    except Exception as exc:  # noqa: BLE001 - protocol boundary
        sys.stdout.write(json.dumps({"type": "worker_error", "error": str(exc)}) + "\n")
        sys.stdout.flush()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
