"""Heuristic action ranking and sibling collapse.

Ranking decides exploration *order* (high-value product flows first);
sibling collapse keeps the graph clean by exploring one representative of
each group of structurally identical elements (blog cards, table rows).
No LLM is involved; an LLM ranker for near-ties arrives in M2.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from engine.identity import normalize_url, strip_positional_selector
from engine.schemas import Interactable

# High-value product flows the mapper should reach early.
_FLOW_KEYWORDS = re.compile(
    r"\b(sign\s?up|register|get\s+started|start\s+free|try\s+(it\s+)?free"
    r"|log\s?in|sign\s?in|pricing|plans?|checkout|onboarding|dashboard"
    r"|settings|billing|invite|upgrade|trial|demo|features|docs|documentation)\b",
    re.I,
)

# Links that rarely reveal product structure.
_LOW_VALUE = re.compile(
    r"\b(privacy|terms|legal|cookie|imprint|careers|press)\b"
    r"|twitter|facebook|linkedin|instagram|youtube|tiktok",
    re.I,
)

_DIGIT_RUN = re.compile(r"\d+")

_SCORE_BASE = 10.0
_SCORE_FLOW_KEYWORD = 40.0
_SCORE_LOW_VALUE = -20.0
_SCORE_NAV_PLACEMENT = 15.0
_SCORE_PRIMARY_CTA = 10.0
_SCORE_NEW_URL = 10.0
_SCORE_VISITED_URL = -30.0
_SCORE_MAIN_FOLD0 = 8.0
_SCORE_FOOTER = -15.0
_SCORE_ASIDE_NAV = 12.0
_SCORE_AUTH_ENTRY = 25.0  # on top of flow keyword — reach login before content nav

_AUTH_ENTRY = re.compile(r"\b(log\s?in|sign\s?in|sign\s?up|register|create\s+account)\b", re.I)


@dataclass
class ActionCandidate:
    """An interactable chosen to represent its sibling group, with a score."""

    interactable: Interactable
    collapsed_count: int = 1
    score: float = 0.0
    grouped_labels: list[str] = field(default_factory=list)
    # Set only for repeated, content-like link cohorts whose URLs differ in
    # parameter positions (for example /game/123/foo and /game/456/bar).
    family_pattern: str | None = None
    family_id: str | None = None
    family_label: str | None = None
    family_kind: str | None = None
    family_status: str | None = None


def loose_url_pattern(url: str) -> str:
    """URL pattern for grouping only (not identity): digit runs collapse so
    /post1 and /post2 group together even when segments aren't pure ids."""
    return _DIGIT_RUN.sub("#", normalize_url(url))


def _group_key(item: Interactable) -> tuple[str, str, str, str]:
    target = (
        loose_url_pattern(item.href)
        if item.href
        else (item.text or item.aria_label or "").strip().lower()
    )
    return (item.tag, item.role or "", strip_positional_selector(item.selector), target)


def group_signature(item: Interactable) -> str:
    """Stable hash of an item's sibling-group key.

    Items that ``collapse_siblings`` would fold together share this id, so the
    UI can group structurally identical surface items (cards, rows, tabs).
    """
    return hashlib.sha1("|".join(_group_key(item)).encode("utf-8")).hexdigest()[:12]


def is_auth_entry(candidate: ActionCandidate) -> bool:
    """True when the action likely opens a login/signup flow."""
    item = candidate.interactable
    href_path = urlsplit(item.href).path if item.href else None
    haystack = " ".join(filter(None, [item.text, item.aria_label, href_path]))
    return bool(_AUTH_ENTRY.search(haystack))


def collapse_siblings(
    items: list[Interactable], *, preserve_item_ids: set[str] | None = None
) -> list[ActionCandidate]:
    """Group structurally identical elements with the same target pattern;
    keep the first of each group as the representative.

    Promoted URL-family members are supplied through ``preserve_item_ids`` and
    remain separate until the run-level registry validates their destinations.
    """
    preserved = preserve_item_ids or set()
    groups: dict[tuple, ActionCandidate] = {}
    candidates: list[ActionCandidate] = []
    for item in items:
        if (item.item_id or item.selector) in preserved:
            candidates.append(
                ActionCandidate(
                    interactable=item,
                    grouped_labels=[item.label],
                )
            )
            continue
        key = _group_key(item)
        if key in groups:
            candidate = groups[key]
            candidate.collapsed_count += 1
            candidate.grouped_labels.append(item.label)
        else:
            groups[key] = ActionCandidate(interactable=item, grouped_labels=[item.label])
    return [*groups.values(), *candidates]


def score_action(candidate: ActionCandidate, *, visited_urls: set[str]) -> float:
    """Heuristic priority score (roughly 0-100); higher explores sooner."""
    item = candidate.interactable
    score = _SCORE_BASE

    # Only the href *path* participates in keyword matching: the host would
    # otherwise leak bonuses (e.g. every link on signup.example.com).
    href_path = urlsplit(item.href).path if item.href else None
    haystack = " ".join(filter(None, [item.text, item.aria_label, href_path]))
    if _FLOW_KEYWORDS.search(haystack):
        score += _SCORE_FLOW_KEYWORD
    if _AUTH_ENTRY.search(haystack):
        score += _SCORE_AUTH_ENTRY
    if _LOW_VALUE.search(haystack):
        score += _SCORE_LOW_VALUE

    if item.in_nav:
        score += _SCORE_NAV_PLACEMENT
    is_button = item.tag == "button" or item.role == "button"
    if is_button and item.fold == 0:
        score += _SCORE_PRIMARY_CTA

    # Viewport-grounded placement: prefer primary content above the fold,
    # de-prioritize footer boilerplate.
    if item.region == "main" and item.fold == 0:
        score += _SCORE_MAIN_FOLD0
    if item.region == "aside" and item.kind in {"link", "menuitem"}:
        score += _SCORE_ASIDE_NAV
    if item.region == "footer":
        score += _SCORE_FOOTER

    if item.href and not item.href.startswith("javascript:"):
        score += _SCORE_VISITED_URL if normalize_url(item.href) in visited_urls else _SCORE_NEW_URL

    return score
