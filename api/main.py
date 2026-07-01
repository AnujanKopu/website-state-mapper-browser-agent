"""FastAPI application wiring: lifespan, CORS, routes, static artifacts.

Run with:
    uv run uvicorn api.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import update

from api.manager import RunManager
from api.routes import router
from engine.config import Settings, load_run_config
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import RunConfig
from engine.storage import LocalStorage


class ArtifactStaticFiles(StaticFiles):
    """Serve immutable screenshot artifacts with long-lived browser caching."""

    async def get_response(self, path: str, scope: dict):
        response = await super().get_response(path, scope)
        normalized_path = path.replace("\\", "/")
        if response.status_code == 200 and "/screenshots/" in f"/{normalized_path}":
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


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
        # Explorations are in-memory tasks and cannot survive an API restart.
        # Reconcile persisted transient statuses before serving them to the UI.
        async with app.state.session_factory.begin() as session:
            await session.execute(
                update(db.Run)
                .where(db.Run.status.in_(["queued", "running", "paused"]))
                .values(
                    status="cancelled",
                    finished_at=datetime.now(UTC),
                    error="Exploration was interrupted by an API restart.",
                )
            )
        app.state.manager = RunManager(
            settings,
            run_config,
            session_factory=app.state.session_factory,
            store=LocalStorage(settings.data_dir),
        )
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
    # relative): e.g. node.screenshot -> /artifacts/runs/<id>/screenshots/<x>.webp
    app.mount("/artifacts", ArtifactStaticFiles(directory=settings.data_dir), name="artifacts")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
