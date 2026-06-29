from pathlib import Path

from PIL import Image

from api.manager import RunManager
from engine.config import Settings
from engine.db import models as db
from engine.db.session import create_db_engine, create_session_factory, init_db
from engine.schemas import RunConfig


async def test_worker_snapshot_is_committed_and_only_pngs_are_published(tmp_path: Path):
    run_id = "a" * 32
    job_root = tmp_path / "jobs"
    job_dir = job_root / run_id
    job_dir.mkdir(parents=True)
    source_database = job_dir / "flowstate.db"
    source_engine = create_db_engine(
        f"sqlite+aiosqlite:///{source_database.as_posix()}"
    )
    await init_db(source_engine)
    source_sessions = create_session_factory(source_engine)
    async with source_sessions() as session:
        session.add(db.Run(id=run_id, url="https://example.test", status="running"))
        session.add(
            db.StateNode(
                id="b" * 32,
                run_id=run_id,
                fingerprint="fingerprint-0001",
                url="https://example.test/",
                url_normalized="https://example.test/",
                title="Example",
                screenshot_path=f"runs/{run_id}/screenshots/state.png",
                dom_snapshot_path=f"runs/{run_id}/dom/state.html",
                text_hash="text",
            )
        )
        await session.commit()
    await source_engine.dispose()

    screenshots = job_dir / "artifacts" / "runs" / run_id / "screenshots"
    screenshots.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="white").save(screenshots / "state.png")
    dom = job_dir / "artifacts" / "runs" / run_id / "dom"
    dom.mkdir()
    (dom / "state.html").write_text("<html>private</html>")

    central_database = tmp_path / "central.db"
    central_engine = create_db_engine(
        f"sqlite+aiosqlite:///{central_database.as_posix()}"
    )
    await init_db(central_engine)
    central_sessions = create_session_factory(central_engine)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{central_database.as_posix()}",
        data_dir=tmp_path / "public-artifacts",
        hosted_mode=True,
        supervisor_url="http://127.0.0.1:8091",
        worker_job_root=job_root,
    )
    manager = RunManager(settings, RunConfig(), session_factory=central_sessions)

    await manager._import_worker_snapshot(run_id)

    async with central_sessions() as session:
        assert await session.get(db.Run, run_id) is not None
        assert await session.get(db.StateNode, "b" * 32) is not None
    assert (
        settings.data_dir / "runs" / run_id / "screenshots" / "state.png"
    ).exists()
    assert not (settings.data_dir / "runs" / run_id / "dom" / "state.html").exists()
    await central_engine.dispose()
