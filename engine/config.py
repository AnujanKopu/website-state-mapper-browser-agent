"""Application settings (environment) and run configuration (YAML) loading."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from engine.schemas import RunConfig


class Settings(BaseSettings):
    """Process-level settings, sourced from environment variables / .env.

    Prefixed with FLOWSTATE_, e.g. FLOWSTATE_DATABASE_URL.
    """

    database_url: str = "sqlite+aiosqlite:///data/flowstate.db"
    data_dir: Path = Path("data")
    run_config_path: Path = Path("config/default_run.yaml")

    model_config = SettingsConfigDict(
        env_prefix="FLOWSTATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def load_run_config(path: Path) -> RunConfig:
    """Load and validate a run configuration YAML.

    Falls back to defaults if the file does not exist, so the engine is
    runnable from a bare checkout.
    """
    if not path.exists():
        return RunConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RunConfig.model_validate(raw)
