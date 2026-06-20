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
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuthMode(StrEnum):
    """How a run should treat authentication boundaries."""

    GUEST = "guest"
    LOGIN = "login"


class AuthContext(StrEnum):
    """Session context that participates in state identity."""

    GUEST = "guest"
    AUTHENTICATED = "authenticated"
    UNKNOWN = "unknown"


class PageRole(StrEnum):
    """Organizational role of an observed state, separate from StateType."""

    HOME = "home"
    HUB = "hub"
    DETAIL = "detail"
    RESULTS = "results"
    FLOW_STEP = "flow_step"
    BOUNDARY = "boundary"


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
    # When False, the raw DOM HTML snapshot is not written to storage.
    # Screenshots and graph metadata are unaffected. Useful for UI-driven
    # runs that only consume screenshots + graph JSON.
    save_dom_snapshots: bool = True
    # Viewport-grounded discovery scrolls the page down in ~90%-viewport
    # steps so below-the-fold affordances are found too. 0 = current
    # viewport only; each extra step records items with a higher fold index.
    max_scroll_steps: int = 4


class BudgetConfig(StrictModel):
    max_states: int = 60
    max_actions: int = 150
    max_depth: int = 4
    max_wall_seconds: int = 300


class ExplorationConfig(StrictModel):
    # Out-edges enqueued per state (top-K after ranking); keeps the graph clean.
    max_actions_per_state: int = 12
    action_timeout_ms: int = 5_000
    # Cap on distinct states kept per loose URL family (e.g. /post/#) so blog
    # archives and card grids can't dominate the graph; further siblings fold
    # into the family representative as skipped surface items + inferred edges.
    url_family_cap: int = 3
    # Login mode may inspect a few header/menu controls to reveal a hidden
    # authentication entry before general exploration starts.
    auth_discovery_action_cap: int = 6
    # Same-URL interactions do not consume page depth, but remain bounded.
    max_substate_depth: int = 2


class AuthenticationConfig(StrictModel):
    mode: AuthMode = AuthMode.GUEST


class RunConfig(StrictModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)


# --------------------------------------------------------------------------
# Capture artifacts
# --------------------------------------------------------------------------


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class SurfaceStatus(StrEnum):
    """Exploration outcome for one discovered surface item."""

    PENDING = "pending"
    EXPLORED = "explored"
    BLOCKED = "blocked"
    NOOP = "noop"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class Interactable(BaseModel):
    """A visible, actionable element discovered on a page.

    Geometry comes in two flavours: ``bounding_box`` is the viewport-relative
    rect at the moment of discovery, while ``page_box`` is the absolute
    document-space rect (stable across scrolling, aligned with the full-page
    screenshot for VLM grounding later).
    """

    selector: str
    tag: str
    role: str | None = None
    text: str | None = None
    aria_label: str | None = None
    aria_selected: bool | None = None
    aria_expanded: bool | None = None
    title: str | None = None
    test_id: str | None = None
    context_label: str | None = None
    href: str | None = None
    bounding_box: BoundingBox
    page_box: BoundingBox | None = None
    in_nav: bool = False
    in_form: bool = False
    in_modal: bool = False
    # Surface-item metadata (viewport-grounded discovery, Slice 3).
    item_id: str = ""
    region: str | None = None  # nav | header | footer | aside | modal | main
    kind: str | None = None  # link | button | tab | menuitem | select | toggle | disclosure
    fold: int = 0  # scroll step at which it first became visible (0 = above fold)
    group_id: str | None = None  # shared by structurally identical siblings
    # Stable semantic signatures used to recognize the same transition
    # capability across re-observations and persistent navigation surfaces.
    control_key: str = ""
    container_key: str | None = None
    status: SurfaceStatus = SurfaceStatus.PENDING

    @property
    def label(self) -> str:
        """Best human-readable name for this element."""
        if self.text or self.aria_label or self.title:
            return self.text or self.aria_label or self.title or ""
        if self.context_label:
            verb = "Open" if self.kind in {"button", "menuitem", "disclosure"} else "View"
            return f"{verb} {self.context_label}"
        if self.test_id:
            return self.test_id.replace("-", " ").replace("_", " ").strip().title()
        return self.href or f"Unlabelled {self.tag}"


class PageSignals(BaseModel):
    """Structural signals extracted in-page, used for state classification."""

    modal_open: bool = False
    password_fields: int = 0
    username_fields: int = 0
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
    auth_context: AuthContext = AuthContext.UNKNOWN


class Credentials(BaseModel):
    """In-memory credentials for auth form autofill. Never persisted to disk."""

    username: str | None = None
    password: str | None = None


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
