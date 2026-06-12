"""Shared event vocabulary for the exploration stream.

The engine emits semantic events using these type names; the API layer
(`api.events` / `api.manager`) wraps them in the SSE transport envelope
(`event_id`, `run_id`, `sequence`, `timestamp`, `type`, `payload`).

Keeping the vocabulary here (not in `api/`) preserves the layering rule:
the explorer never imports the API, but both agree on the contract.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """The SSE event vocabulary (contract v1)."""

    RUN_STARTED = "run_started"
    STATE_DISCOVERED = "state_discovered"
    EDGE_DISCOVERED = "edge_discovered"
    STATE_UPDATED = "state_updated"
    SURFACE_ITEMS_DISCOVERED = "surface_items_discovered"
    ACTION_STARTED = "action_started"
    ACTION_FINISHED = "action_finished"
    FRONTIER_UPDATED = "frontier_updated"
    AUTH_GATE = "auth_gate"
    STATE_LABELED = "state_labeled"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    HEARTBEAT = "heartbeat"


class ActionOutcome(StrEnum):
    """Result of popping and attempting one frontier action."""

    NEW_STATE = "new_state"
    DEDUPED = "deduped"
    NOOP = "noop"
    FAILED = "failed"
    BLOCKED = "blocked"


# Terminal events close the SSE stream; no further events follow.
TERMINAL_EVENTS = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})
