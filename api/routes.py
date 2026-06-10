"""HTTP + SSE routes. Thin: they validate input and delegate to services."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sse_starlette.sse import EventSourceResponse

from api.manager import RunHandle, RunManager
from api.schemas import CreateRunRequest, CreateRunResponse, RunStatusResponse
from engine.db import models as db
from engine.export import export_graph

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
    handle = manager.start_run(body.url, overrides=body.overrides())
    return CreateRunResponse(
        run_id=handle.run_id,
        url=handle.url,
        status=handle.status.value,
        events_url=f"/api/runs/{handle.run_id}/events",
        graph_url=f"/api/runs/{handle.run_id}/graph",
    )


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
        return RunStatusResponse(
            run_id=row.id,
            url=row.url,
            status=row.status,
            error=row.error,
            stats=row.stats,
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )

    # Handle exists but the run row hasn't been written yet (just started).
    return RunStatusResponse(run_id=handle.run_id, url=handle.url, status=handle.status.value)


@router.get("/runs/{run_id}/graph")
async def get_graph(run_id: str, sessions: SessionsDep) -> dict:
    """Full state graph (states + edges) for a run, as it stands now."""
    try:
        return await export_graph(sessions, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str, request: Request, manager: ManagerDep
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
    return EventSourceResponse(_event_source(manager, handle, request))


async def _event_source(
    manager: RunManager, handle: RunHandle, request: Request
) -> AsyncIterator[dict]:
    async for event in manager.subscribe(handle):
        if await request.is_disconnected():
            break
        yield {
            "id": str(event.seq),
            "event": event.kind,
            "data": json.dumps({"message": event.message, "data": event.data}),
        }
