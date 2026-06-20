"""Deterministic state naming and organizational roles (LLM-free)."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit

from engine.classify import StateAnalysis
from engine.schemas import Observation, PageRole, StateType

_RESULTS = re.compile(r"\b(search|results?|matches)\b", re.I)
_DETAIL_SEGMENTS = re.compile(
    r"\b(game|video|product|post|profile|user|item|listing|article)\b", re.I
)
_BOUNDARIES = {
    StateType.AUTH_WALL,
    StateType.PAYWALL,
    StateType.RISKY_TERMINAL,
    StateType.DEAD_END,
    StateType.EXTERNAL,
}


def clean_title(title: str, url: str) -> str:
    """Remove common site-brand suffixes and provide a readable path fallback."""
    value = " ".join((title or "").split()).strip()
    if " | " in value:
        value = value.split(" | ", 1)[0].strip()
    if value:
        return value
    path = unquote(urlsplit(url).path).strip("/")
    return (path.rsplit("/", 1)[-1] or "Home").replace("-", " ").title()


def infer_page_role(
    observation: Observation,
    analysis: StateAnalysis,
    state_type: StateType,
    *,
    depth: int,
    route_family: str | None,
) -> PageRole:
    if state_type in _BOUNDARIES:
        return PageRole.BOUNDARY
    if state_type in {StateType.MODAL, StateType.FORM, StateType.WIZARD_STEP, StateType.TAB}:
        return PageRole.FLOW_STEP
    if depth == 0:
        return PageRole.HOME
    title_and_url = f"{observation.snapshot.title} {observation.url_normalized}"
    if _RESULTS.search(title_and_url) or "?q=" in observation.url_normalized:
        return PageRole.RESULTS
    if route_family or _DETAIL_SEGMENTS.search(urlsplit(observation.url_normalized).path):
        return PageRole.DETAIL
    if any(candidate.family_pattern for candidate in analysis.candidates):
        return PageRole.HUB
    return PageRole.FLOW_STEP


def heuristic_name(
    observation: Observation,
    *,
    state_type: StateType,
    trigger_label: str | None,
    parent_label: str | None,
) -> dict:
    base = clean_title(observation.snapshot.title, observation.snapshot.url)
    if trigger_label and parent_label and state_type in {
        StateType.TAB,
        StateType.DROPDOWN,
        StateType.MODAL,
        StateType.FORM,
        StateType.WIZARD_STEP,
    }:
        text = f"{parent_label} — {trigger_label}"
    elif state_type == StateType.AUTH_WALL:
        text = base if re.search(r"log|sign|auth", base, re.I) else "Authentication"
    else:
        text = base
    key = hashlib.sha1(
        f"{state_type.value}|{observation.url_normalized}|{trigger_label or ''}".encode()
    ).hexdigest()[:12]
    return {
        "text": text,
        "source": "heuristic",
        "confidence": 0.82 if trigger_label or observation.snapshot.title else 0.65,
        "key": key,
    }
