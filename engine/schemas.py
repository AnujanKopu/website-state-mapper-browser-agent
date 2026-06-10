"""Pydantic domain models shared across the engine.

These are the in-memory representations; persistence lives in `engine.db.models`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StateType(StrEnum):
    """Classification of a captured state. Only PAGE is produced in M0;
    the remaining types are assigned by the classifier from M1 onward."""

    PAGE = "page"
    MODAL = "modal"
    FORM = "form"
    AUTH_WALL = "auth_wall"
    PAYWALL = "paywall"
    DROPDOWN = "dropdown"
    TAB = "tab"
    WIZARD_STEP = "wizard_step"
    ERROR = "error"
    DEAD_END = "dead_end"
    RISKY_TERMINAL = "risky_terminal"
    EXTERNAL = "external"


# --------------------------------------------------------------------------
# Run configuration (loaded from config/default_run.yaml)
# --------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Base for config models: reject unknown keys so YAML typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class ViewportConfig(StrictModel):
    width: int = 1366
    height: int = 900


class BrowserConfig(StrictModel):
    headless: bool = True
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    user_agent: str | None = None
    navigation_timeout_ms: int = 20_000
    stabilize_quiet_ms: int = 500


class CaptureConfig(StrictModel):
    full_page_screenshot: bool = True
    max_interactables: int = 100
    max_visible_text_chars: int = 20_000


class BudgetConfig(StrictModel):
    max_states: int = 60
    max_actions: int = 150
    max_depth: int = 4
    max_wall_seconds: int = 300


class RunConfig(StrictModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)


# --------------------------------------------------------------------------
# Capture artifacts
# --------------------------------------------------------------------------


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class Interactable(BaseModel):
    """A visible, actionable element discovered on a page."""

    selector: str
    tag: str
    role: str | None = None
    text: str | None = None
    aria_label: str | None = None
    href: str | None = None
    bounding_box: BoundingBox

    @property
    def label(self) -> str:
        """Best human-readable name for this element."""
        return self.text or self.aria_label or self.href or f"<{self.tag}>"


class PageSnapshot(BaseModel):
    """Raw observation of the current page, before identity or storage."""

    url: str
    title: str
    visible_text: str
    html: str
    screenshot_png: bytes


class CapturedState(BaseModel):
    """A fully processed state: snapshot + identity + persisted artifact paths."""

    state_id: str
    run_id: str
    url: str
    url_normalized: str
    title: str
    fingerprint: str
    text_hash: str
    state_type: StateType = StateType.PAGE
    visible_text: str
    interactables: list[Interactable]
    screenshot_path: str
    dom_snapshot_path: str
    depth: int = 0
