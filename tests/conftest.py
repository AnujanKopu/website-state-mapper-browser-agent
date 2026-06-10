from pathlib import Path

import pytest

from engine.config import Settings

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "demo_site"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: throwaway SQLite db + artifact dir per test."""
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "artifacts",
        run_config_path=tmp_path / "missing.yaml",  # falls back to RunConfig defaults
    )
