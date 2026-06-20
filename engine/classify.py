"""Heuristic state classification (no LLM).

Combines page signals (extracted in-browser), visible text patterns, and
the safety verdicts on the state's candidate actions to assign a StateType
and a flags dict. An LLM/VLM labeler refines labels in M2; it never
overrides these structural classifications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.ranking import ActionCandidate, collapse_siblings
from engine.safety import SafetyDecision, evaluate_action
from engine.schemas import Observation, PageSignals, StateType

_PRICE = re.compile(r"[$\u20ac\u00a3]\s?\d")
_PAYWALL_WORDS = re.compile(r"\b(upgrade|premium|unlock|subscribe|members?\s+only)\b", re.I)
_LOGIN_WORDS = re.compile(r"\b(log\s?in|sign\s?in|password|authenticate)\b", re.I)


@dataclass
class StateAnalysis:
    """Candidate actions for a state, split by safety verdict, plus its type."""

    candidates: list[ActionCandidate]
    safe: list[ActionCandidate]
    denied: list[tuple[ActionCandidate, SafetyDecision]]
    state_type: StateType
    flags: dict


def classify_state(
    signals: PageSignals,
    visible_text: str,
    *,
    total_candidates: int,
    safe_candidates: int,
    denied: list[tuple[ActionCandidate, SafetyDecision]],
) -> tuple[StateType, dict]:
    """Assign a state type and detection flags from structural evidence."""
    flags = {
        "modal_open": signals.modal_open,
        "auth_required": signals.password_fields > 0 or signals.username_fields > 0,
        "payment_required": signals.payment_fields > 0
        or any(d.category and d.category.value == "payment" for _, d in denied),
        "form_count": signals.form_count,
        "dead_end": total_candidates == 0,
        "risky_terminal": total_candidates > 0 and safe_candidates == 0,
        "denied_actions": [
            {
                "label": candidate.interactable.label,
                "category": decision.category.value if decision.category else None,
                "reason": decision.reason,
            }
            for candidate, decision in denied
        ],
    }

    if total_candidates == 0:
        state_type = StateType.DEAD_END
    elif safe_candidates == 0:
        state_type = StateType.RISKY_TERMINAL
    elif signals.modal_open:
        state_type = StateType.MODAL
    elif (
        (signals.password_fields > 0 or signals.username_fields > 0)
        and signals.form_count > 0
        and _LOGIN_WORDS.search(visible_text)
    ):
        state_type = StateType.AUTH_WALL
    elif signals.payment_fields > 0 or (
        _PRICE.search(visible_text) and _PAYWALL_WORDS.search(visible_text)
    ):
        state_type = StateType.PAYWALL
    else:
        state_type = StateType.PAGE

    return state_type, flags


def analyze_state(observation: Observation, *, base_url: str) -> StateAnalysis:
    """Full candidate pipeline for one observed state:
    collapse siblings -> modal scoping -> safety verdicts -> classification.
    """
    candidates = collapse_siblings(observation.interactables)

    # When a modal is open it owns the interaction: elements behind the
    # backdrop are not actionable (clicks would be intercepted anyway).
    if observation.snapshot.signals.modal_open:
        in_modal = [c for c in candidates if c.interactable.in_modal]
        if in_modal:
            candidates = in_modal

    safe: list[ActionCandidate] = []
    denied: list[tuple[ActionCandidate, SafetyDecision]] = []
    for candidate in candidates:
        decision = evaluate_action(candidate.interactable, base_url=base_url)
        if decision.allowed:
            safe.append(candidate)
        else:
            denied.append((candidate, decision))

    state_type, flags = classify_state(
        observation.snapshot.signals,
        observation.snapshot.visible_text,
        total_candidates=len(candidates),
        safe_candidates=len(safe),
        denied=denied,
    )
    return StateAnalysis(
        candidates=candidates, safe=safe, denied=denied, state_type=state_type, flags=flags
    )
