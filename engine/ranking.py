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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def loose_url_pattern(url: str) -> str:
    """URL pattern for grouping only (not identity): digit runs collapse so
    /post1 and /post2 group together even when segments aren't pure ids."""
    return _DIGIT_RUN.sub("#", normalize_url(url))


def _href_shape(item: Interactable) -> tuple[str, str, int, tuple[str, ...]] | None:
    """Coarse shape used only to find comparable repeated-link cohorts."""
    if not item.href or item.in_nav or item.region in {"nav", "header", "footer"}:
        return None
    parts = urlsplit(normalize_url(item.href))
    if parts.scheme not in {"http", "https", "file"}:
        return None
    segments = tuple(segment for segment in parts.path.split("/") if segment)
    query_keys = tuple(sorted(name for name, _ in parse_qsl(parts.query, keep_blank_values=True)))
    return parts.scheme, parts.netloc, len(segments), query_keys


def _infer_family_pattern(items: list[Interactable]) -> str | None:
    """Infer a parameterized route shared by a repeated selector cohort.

    Literal positions are preserved and only positions that vary across the
    cohort become ``:param``. Requiring a shared literal path segment avoids
    treating unrelated one-segment routes such as /about and /pricing as one
    content family.
    """
    if len(items) < 2:
        return None
    normalized = [urlsplit(normalize_url(item.href or "")) for item in items]
    segments = [
        tuple(segment for segment in part.path.split("/") if segment)
        for part in normalized
    ]
    if not segments or any(len(row) != len(segments[0]) for row in segments):
        return None

    pattern_segments: list[str] = []
    shared_literals = 0
    varied = False
    for column in zip(*segments, strict=True):
        if len(set(column)) == 1:
            value = column[0]
            pattern_segments.append(value)
            if value not in {":id", "#"}:
                shared_literals += 1
        else:
            pattern_segments.append(":param")
            varied = True
    if not varied or shared_literals == 0:
        return None

    query_rows = [dict(parse_qsl(part.query, keep_blank_values=True)) for part in normalized]
    query_keys = sorted(query_rows[0]) if query_rows else []
    pattern_query: list[tuple[str, str]] = []
    for key in query_keys:
        values = {row.get(key, "") for row in query_rows}
        pattern_query.append((key, next(iter(values)) if len(values) == 1 else ":param"))

    first = normalized[0]
    path = "/" + "/".join(pattern_segments) if pattern_segments else "/"
    return urlunsplit((first.scheme, first.netloc, path, urlencode(pattern_query, safe=":"), ""))


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


def collapse_siblings(items: list[Interactable]) -> list[ActionCandidate]:
    """Group structurally identical elements with the same target pattern;
    keep the first of each group as the representative.

    Slug/username families need structural confirmation, so their first few
    links remain separate candidates. They share a family pattern and group
    id, allowing the explorer to sample them up to the configured family cap.
    """
    selector_cohorts: dict[tuple, list[Interactable]] = {}
    for item in items:
        shape = _href_shape(item)
        if shape is None:
            continue
        selector_key = (item.tag, item.role or "", strip_positional_selector(item.selector))
        selector_cohorts.setdefault((*selector_key, *shape), []).append(item)

    family_by_item: dict[str, tuple[str, int, list[str]]] = {}
    for cohort in selector_cohorts.values():
        pattern = _infer_family_pattern(cohort)
        if pattern is None:
            continue
        group_id = hashlib.sha1(f"route-family|{pattern}".encode()).hexdigest()[:12]
        labels = [item.label for item in cohort]
        for item in cohort:
            item.group_id = group_id
            family_by_item[item.item_id or item.selector] = (pattern, len(cohort), labels)

    groups: dict[tuple, ActionCandidate] = {}
    candidates: list[ActionCandidate] = []
    family_seen: set[str] = set()
    for item in items:
        family = family_by_item.get(item.item_id or item.selector)
        if family is not None:
            pattern, family_size, labels = family
            # Keep members as separate candidates so the explorer can observe
            # and compare a bounded number of examples. The first candidate's
            # count describes the full discovered cohort in graph output.
            candidates.append(
                ActionCandidate(
                    interactable=item,
                    collapsed_count=family_size if pattern not in family_seen else 1,
                    grouped_labels=labels if pattern not in family_seen else [item.label],
                    family_pattern=pattern,
                )
            )
            family_seen.add(pattern)
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
    if item.region == "footer":
        score += _SCORE_FOOTER

    if item.href and not item.href.startswith("javascript:"):
        score += (
            _SCORE_VISITED_URL if normalize_url(item.href) in visited_urls else _SCORE_NEW_URL
        )

    return score
