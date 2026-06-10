"""FastAPI application wiring: lifespan, CORS, routes, static artifacts.

Run with:
    uv run uvicorn api.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.manager import RunManager
from api.routes import router
from engine.config import Settings, load_run_config
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import RunConfig


def create_app(
    settings: Settings | None = None, run_config: RunConfig | None = None
) -> FastAPI:
    """Build the app. Dependencies are injectable so tests can isolate state."""
    settings = settings or Settings()
    run_config = run_config or load_run_config(settings.run_config_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_db_engine(settings.database_url)
        await init_db(engine)
        app.state.settings = settings
        app.state.session_factory = create_session_factory(engine)
        app.state.manager = RunManager(settings, run_config)
        try:
            yield
        finally:
            await app.state.manager.shutdown()
            await engine.dispose()

    app = FastAPI(title="FlowState API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local dev; tighten before any deployment
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    # Screenshots/DOM snapshots referenced by graph nodes (paths are data-dir
    # relative): e.g. node.screenshot -> /artifacts/runs/<id>/screenshots/<x>.png
    app.mount("/artifacts", StaticFiles(directory=settings.data_dir), name="artifacts")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
