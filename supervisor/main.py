"""Small private service that owns the rootless container runtime.

The public API never receives a runtime socket. Each request launches exactly
one disposable crawl container and streams its JSONL protocol unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

RUN_ID = re.compile(r"^[a-f0-9]{16,64}$")
JOB_ROOT = Path(os.environ.get("FLOWSTATE_WORKER_JOB_ROOT", "data/worker-jobs")).resolve()
JOB_HOST_ROOT = Path(
    os.environ.get("FLOWSTATE_WORKER_JOB_HOST_ROOT", str(JOB_ROOT))
)
PROJECT_DIR = Path(os.environ.get("FLOWSTATE_PROJECT_DIR", ".")).resolve()
WORKER_COMMAND = shlex.split(
    os.environ.get(
        "FLOWSTATE_WORKER_COMMAND",
        "docker compose -f docker-compose.worker.yml run --rm -T --no-deps crawl-worker",
    ),
    posix=os.name != "nt",
)
TOKEN = os.environ.get("FLOWSTATE_SUPERVISOR_TOKEN")
WORKER_HARD_LIFETIME_SECONDS = int(
    os.environ.get("FLOWSTATE_WORKER_HARD_LIFETIME_SECONDS", "3600")
)


@dataclass
class WorkerHandle:
    process: asyncio.subprocess.Process
    job_dir: Path
    started_at: float


app = FastAPI(title="FlowState Private Worker Supervisor", docs_url=None, redoc_url=None)
_runs: dict[str, WorkerHandle] = {}


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HTTPException(status_code=422, detail="only public HTTP(S) URLs are allowed")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise HTTPException(status_code=422, detail="private or reserved hosts are blocked")


def _authorize(value: str | None) -> None:
    if TOKEN and (value is None or not secrets.compare_digest(value, TOKEN)):
        raise HTTPException(status_code=403, detail="invalid supervisor token")


def _job_dir(run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=422, detail="invalid run id")
    path = (JOB_ROOT / run_id).resolve()
    if JOB_ROOT not in path.parents:
        raise HTTPException(status_code=422, detail="invalid job path")
    return path


async def _send(handle: WorkerHandle, payload: dict) -> None:
    if handle.process.stdin is None or handle.process.returncode is not None:
        raise HTTPException(status_code=409, detail="worker is not accepting commands")
    handle.process.stdin.write(
        (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    )
    await handle.process.stdin.drain()


async def _drain_stderr(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    # Prevent a noisy runtime from blocking. Worker protocol never uses stderr.
    while await stream.readline():
        pass


@app.post("/private/runs/{run_id}/start")
async def start_run(
    run_id: str,
    body: dict,
    x_flowstate_supervisor_token: str | None = Header(default=None),
) -> StreamingResponse:
    _authorize(x_flowstate_supervisor_token)
    _validate_url(str(body.get("url") or ""))
    if run_id in _runs:
        raise HTTPException(status_code=409, detail="run already exists")
    job_dir = _job_dir(run_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "FLOWSTATE_JOB_DIR": str(JOB_HOST_ROOT / run_id),
    }
    process = await asyncio.create_subprocess_exec(
        *WORKER_COMMAND,
        cwd=PROJECT_DIR,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    handle = WorkerHandle(process=process, job_dir=job_dir, started_at=time.monotonic())
    _runs[run_id] = handle
    await _send(
        handle,
        {
            "type": "start",
            "run_id": run_id,
            "url": body["url"],
            "config": body.get("config") or {},
            "credentials": body.get("credentials"),
            # Raw DOM remains ephemeral and is never requested by hosted API.
            "save_dom_snapshots": False,
        },
    )

    async def output():
        stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
        try:
            assert process.stdout is not None
            while True:
                remaining = WORKER_HARD_LIFETIME_SECONDS - (
                    time.monotonic() - handle.started_at
                )
                if remaining <= 0:
                    yield (
                        json.dumps(
                            {
                                "type": "worker_error",
                                "error": "hosted worker hard lifetime exceeded",
                            }
                        )
                        + "\n"
                    ).encode()
                    break
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=remaining
                    )
                except TimeoutError:
                    yield (
                        json.dumps(
                            {
                                "type": "worker_error",
                                "error": "hosted worker hard lifetime exceeded",
                            }
                        )
                        + "\n"
                    ).encode()
                    break
                if not line:
                    break
                yield line
            if process.returncode is None and (
                time.monotonic() - handle.started_at
            ) < WORKER_HARD_LIFETIME_SECONDS:
                await process.wait()
        finally:
            if process.returncode is None:
                with contextlib.suppress(Exception):
                    await _send(handle, {"type": "cancel"})
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.terminate()
                    with contextlib.suppress(Exception):
                        await process.wait()
            stderr_task.cancel()
            _runs.pop(run_id, None)

    return StreamingResponse(output(), media_type="application/x-ndjson")


@app.post("/private/runs/{run_id}/command", status_code=202)
async def command_run(
    run_id: str,
    body: dict,
    x_flowstate_supervisor_token: str | None = Header(default=None),
) -> dict:
    _authorize(x_flowstate_supervisor_token)
    handle = _runs.get(run_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="unknown live worker")
    kind = body.get("type")
    if kind not in {"auth_resume", "auth_skip", "cancel"}:
        raise HTTPException(status_code=422, detail="invalid worker command")
    await _send(
        handle,
        {
            "type": kind,
            **(
                {"credentials": body.get("credentials")}
                if kind == "auth_resume"
                else {}
            ),
        },
    )
    return {"status": "accepted"}


@app.delete("/private/runs/{run_id}")
async def cleanup_run(
    run_id: str,
    x_flowstate_supervisor_token: str | None = Header(default=None),
) -> dict:
    _authorize(x_flowstate_supervisor_token)
    if run_id in _runs:
        raise HTTPException(status_code=409, detail="worker is still running")
    job_dir = _job_dir(run_id)
    if job_dir.exists():
        shutil.rmtree(job_dir)
    return {"status": "removed"}
