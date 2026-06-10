"""Typed request/response models for the API."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


class CreateRunRequest(BaseModel):
    """Start an exploration. Budget fields override config/default_run.yaml."""

    url: str
    headless: bool | None = None
    max_states: int | None = Field(default=None, ge=1)
    max_actions: int | None = Field(default=None, ge=1)
    max_depth: int | None = Field(default=None, ge=0)
    max_wall_seconds: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        # Bare hosts (example.com) default to https; explicit schemes pass through.
        return value if _HAS_SCHEME.match(value) else f"https://{value}"

    def overrides(self) -> dict:
        return self.model_dump(exclude={"url"}, exclude_none=True)


class CreateRunResponse(BaseModel):
    run_id: str
    url: str
    status: str
    events_url: str
    graph_url: str


class RunStatusResponse(BaseModel):
    run_id: str
    url: str
    status: str
    error: str | None = None
    stats: dict | None = None
    started_at: str | None = None
    finished_at: str | None = None
