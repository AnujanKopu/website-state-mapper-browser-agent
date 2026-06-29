"""HTTP + SSE routes. Thin: they validate input and delegate to services."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from api.manager import RunHandle, RunManager
from api.schemas import (
    AuthResumeRequest,
    CreateRunRequest,
    CreateRunResponse,
    RunStatusResponse,
    RunSummary,
)
from engine.db import models as db
from engine.export import export_context_pack, export_graph
from engine.schemas import Credentials, RunStatus

router = APIRouter()


def get_manager(request: Request) -> RunManager:
    return request.app.state.manager


def get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


ManagerDep = Annotated[RunManager, Depends(get_manager)]
SessionsDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)]


@router.post("/runs", response_model=CreateRunResponse, status_code=202)
async def create_run(body: CreateRunRequest, manager: ManagerDep) -> CreateRunResponse:
    """Start an exploration in the background and return its identifiers."""
    credentials: Credentials | None = None
    if body.credentials is not None:
        credentials = Credentials(
            username=body.credentials.username,
            password=body.credentials.password,
        )
    try:
        handle = manager.start_run(
            body.url, overrides=body.overrides(), credentials=credentials
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreateRunResponse(
        run_id=handle.run_id,
        url=handle.url,
        status=handle.status.value,
        events_url=f"/api/runs/{handle.run_id}/events",
        graph_url=f"/api/runs/{handle.run_id}/graph",
    )


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(sessions: SessionsDep) -> list[RunSummary]:
    """All runs, newest first -- lets the UI find and rehydrate past runs."""
    async with sessions() as session:
        rows = (
            (await session.execute(select(db.Run).order_by(db.Run.started_at.desc())))
            .scalars()
            .all()
        )
    return [
        RunSummary(
            run_id=row.id,
            url=row.url,
            status=row.status,
            stats=row.stats,
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )
        for row in rows
    ]


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str, manager: ManagerDep, sessions: SessionsDep
) -> RunStatusResponse:
    """Current status of a run: live handle merged with the persisted row."""
    handle = manager.get(run_id)
    async with sessions() as session:
        row = await session.get(db.Run, run_id)

    if handle is None and row is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")

    if row is not None:
        # The live handle may report "paused" (auth gate) before the DB is updated.
        # Prefer the handle's status for transient states like paused.
        live_status = row.status
        if handle is not None and handle.status == RunStatus.PAUSED:
            live_status = RunStatus.PAUSED.value
        return RunStatusResponse(
            run_id=row.id,
            url=row.url,
            status=live_status,
            error=row.error,
            stats=row.stats,
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )

    # Handle exists but the run row hasn't been written yet (just started).
    return RunStatusResponse(run_id=handle.run_id, url=handle.url, status=handle.status.value)


@router.get("/runs/{run_id}/graph")
async def get_graph(run_id: str, manager: ManagerDep, sessions: SessionsDep) -> dict:
    """Full state graph (states + edges) for a run, as it stands now."""
    handle = manager.get(run_id)
    # This is a lower-bound watermark: explorer mutations are committed before
    # their events are published, so every event at or below it is represented
    # by the ensuing database snapshot.
    snapshot_sequence = handle.last_sequence if handle is not None else None
    try:
        graph = await export_graph(sessions, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if handle is not None and handle.status == RunStatus.PAUSED:
        graph["run"]["status"] = RunStatus.PAUSED.value
    terminal = graph["run"]["status"] in {"done", "failed", "cancelled"}
    graph["sync"] = {
        "schema_version": 4,
        "snapshot_sequence": snapshot_sequence,
        "authoritative": handle is None or handle.done or terminal,
        "latest_state_id": graph["states"][-1]["id"] if graph["states"] else None,
    }
    return graph


@router.get("/runs/{run_id}/export")
async def export_run(run_id: str, sessions: SessionsDep) -> JSONResponse:
    """Same graph as `/graph`, delivered as a downloadable JSON attachment."""
    try:
        graph = await export_graph(sessions, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        content=graph,
        headers={"Content-Disposition": f'attachment; filename="flowstate-{run_id}.json"'},
    )


@router.get("/runs/{run_id}/context", response_model=None)
async def get_context_pack(
    run_id: str,
    sessions: SessionsDep,
    format: Annotated[str, Query(pattern="^(markdown|json)$")] = "markdown",
) -> JSONResponse | PlainTextResponse:
    """Deterministic, LLM-free context pack describing the mapped site.

    `format=markdown` (default) returns a readable brief; `format=json`
    returns the structured twin. Both download as attachments.
    """
    try:
        pack = await export_context_pack(sessions, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if format == "json":
        return JSONResponse(
            content=pack["json"],
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flowstate-{run_id}-context.json"'
                )
            },
        )
    return PlainTextResponse(
        content=pack["markdown"],
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="flowstate-{run_id}-context.md"'
        },
    )


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    manager: ManagerDep,
    after_sequence: Annotated[int | None, Query(ge=-1)] = None,
) -> EventSourceResponse:
    """Server-Sent Events stream of the run's progress.

    Replays buffered history then streams live, so subscribing at any time
    yields the complete event sequence.
    """
    handle = manager.get(run_id)
    if handle is None:
        raise HTTPException(
            status_code=404,
            detail=f"No live event stream for run {run_id}; use /graph for results",
        )
    last_event_id = request.headers.get("last-event-id")
    header_sequence = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1
    cursor = max(after_sequence if after_sequence is not None else -1, header_sequence)
    return EventSourceResponse(
        _event_source(manager, handle, request, after_sequence=cursor)
    )


async def _event_source(
    manager: RunManager,
    handle: RunHandle,
    request: Request,
    *,
    after_sequence: int = -1,
) -> AsyncIterator[dict]:
    async for event in manager.subscribe(handle, after_sequence=after_sequence):
        if await request.is_disconnected():
            break
        yield {
            "id": str(event.sequence),
            "event": event.type,
            "data": json.dumps(event.to_envelope()),
        }


# --------------------------------------------------------------------------
# Auth gate endpoints (Slices 5-6)
# --------------------------------------------------------------------------


def _get_paused_handle(run_id: str, manager: RunManager) -> RunHandle:
    """Return the handle for a paused run, or raise 404/409."""
    handle = manager.get(run_id)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"No live run: {run_id}")
    if handle.auth_gate is None:
        raise HTTPException(
            status_code=409, detail=f"Run {run_id} is not paused at an auth gate"
        )
    return handle


@router.post("/runs/{run_id}/auth/resume", status_code=200)
async def auth_resume(
    run_id: str,
    body: AuthResumeRequest,
    manager: ManagerDep,
) -> dict:
    """Resume an exploration paused at an auth wall.

    Optionally supply (or update) credentials for autofill before re-observing
    the page. The browser session remains open; if the user authenticated
    manually in a headed browser the page change will be detected automatically.
    """
    handle = _get_paused_handle(run_id, manager)
    credentials: Credentials | None = None
    if body.credentials is not None:
        credentials = Credentials(
            username=body.credentials.username,
            password=body.credentials.password,
        )
    resolved = handle.resolve_auth_gate("resume", credentials)
    if not resolved:
        raise HTTPException(status_code=409, detail="Auth gate already resolved")
    return {"status": "resumed", "run_id": run_id}


@router.post("/runs/{run_id}/auth/skip", status_code=200)
async def auth_skip(run_id: str, manager: ManagerDep) -> dict:
    """Skip the auth wall and continue exploration without authenticating.

    The auth-wall state is recorded in the graph with a ``auth_gate_skipped``
    flag; no edges are added past it from this decision point.
    """
    handle = _get_paused_handle(run_id, manager)
    resolved = handle.resolve_auth_gate("skip")
    if not resolved:
        raise HTTPException(status_code=409, detail="Auth gate already resolved")
    return {"status": "skipped", "run_id": run_id}
