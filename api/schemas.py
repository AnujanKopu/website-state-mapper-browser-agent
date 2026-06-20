"""Typed request/response models for the API."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from engine.schemas import AuthMode

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


class CredentialsRequest(BaseModel):
    """In-memory credentials for auth form autofill (never persisted to disk)."""

    username: str | None = None
    password: str | None = None


class CreateRunRequest(BaseModel):
    """Start an exploration. Budget fields override config/default_run.yaml."""

    url: str
    headless: bool | None = None
    max_states: int | None = Field(default=None, ge=1)
    max_actions: int | None = Field(default=None, ge=1)
    max_depth: int | None = Field(default=None, ge=0)
    max_wall_seconds: int | None = Field(default=None, ge=1)
    save_dom_snapshots: bool | None = None
    credentials: CredentialsRequest | None = None
    auth_mode: AuthMode = AuthMode.GUEST

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        # Bare hosts (example.com) default to https; explicit schemes pass through.
        return value if _HAS_SCHEME.match(value) else f"https://{value}"

    @model_validator(mode="after")
    def _credentials_require_login_mode(self):
        if self.credentials is not None and self.auth_mode != AuthMode.LOGIN:
            raise ValueError("credentials require auth_mode='login'")
        return self

    def overrides(self) -> dict:
        return self.model_dump(exclude={"url", "credentials"}, exclude_none=True)


class AuthResumeRequest(BaseModel):
    """Optional credentials to supply (or update) when resuming an auth gate."""

    credentials: CredentialsRequest | None = None


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


class RunSummary(BaseModel):
    """One row in the run-listing endpoint."""

    run_id: str
    url: str
    status: str
    stats: dict | None = None
    started_at: str | None = None
    finished_at: str | None = None
