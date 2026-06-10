"""Pydantic domain models shared across the engine.

These are the in-memory representations; persistence lives in `engine.db.models`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StateType(StrEnum):
    """Classification of a captured state."""

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


class ExplorationConfig(StrictModel):
    # Out-edges enqueued per state (top-K after ranking); keeps the graph clean.
    max_actions_per_state: int = 12
    action_timeout_ms: int = 5_000


class RunConfig(StrictModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)


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
    in_nav: bool = False
    in_form: bool = False
    in_modal: bool = False

    @property
    def label(self) -> str:
        """Best human-readable name for this element."""
        return self.text or self.aria_label or self.href or f"<{self.tag}>"


class PageSignals(BaseModel):
    """Structural signals extracted in-page, used for state classification."""

    modal_open: bool = False
    password_fields: int = 0
    payment_fields: int = 0
    form_count: int = 0


class PageSnapshot(BaseModel):
    """Raw observation of the current page, before identity or storage."""

    url: str
    title: str
    visible_text: str
    html: str
    screenshot_png: bytes
    dom_skeleton: str
    signals: PageSignals = Field(default_factory=PageSignals)


class Observation(BaseModel):
    """A snapshot plus everything derived from it (identity, interactables).

    Cheap to compute and not yet persisted -- the explorer deduplicates
    observations against known states before deciding to keep one.
    """

    snapshot: PageSnapshot
    interactables: list[Interactable]
    url_normalized: str
    text_digest: str
    text_simhash: int
    skeleton_hash: str
    action_sig: str
    screenshot_dhash: int
    fingerprint: str


class ActionStep(BaseModel):
    """One replayable step on the path from the run root to a state."""

    kind: Literal["goto", "click"]
    url: str | None = None
    selector: str | None = None
    label: str | None = None


class CapturedState(BaseModel):
    """A fully processed state: observation + persisted artifact paths."""

    state_id: str
    run_id: str
    url: str
    url_normalized: str
    title: str
    fingerprint: str
    text_hash: str
    text_simhash: int = 0
    skeleton_hash: str = ""
    action_sig: str = ""
    screenshot_dhash: int = 0
    signals: PageSignals = Field(default_factory=PageSignals)
    state_type: StateType = StateType.PAGE
    detected_flags: dict = Field(default_factory=dict)
    visible_text: str
    interactables: list[Interactable]
    screenshot_path: str
    dom_snapshot_path: str
    path: list[ActionStep] = Field(default_factory=list)
    depth: int = 0
