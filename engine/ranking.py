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
_SCORE_ASIDE_NAV = 12.0
_SCORE_AUTH_ENTRY = 25.0  # on top of flow keyword — reach login before content nav

_AUTH_ENTRY = re.compile(r"\b(log\s?in|sign\s?in|sign\s?up|register|create\s+account)\b", re.I)
_CONTENT_FAMILY_HINT = re.compile(
    r"\b(game|games|video|videos|product|products|post|posts|blog|article|articles|"
    r"news|profile|profiles|user|users|player|players|item|items|card|cards)\b",
    re.I,
)
_DETAIL_PREFIXES: dict[str, tuple[str, str]] = {
    "game": ("Games", "game"),
    "games": ("Games", "game"),
    "video": ("Videos", "video"),
    "videos": ("Videos", "video"),
    "short": ("Shorts", "video"),
    "shorts": ("Shorts", "video"),
    "watch": ("Videos", "video"),
    "product": ("Products", "product"),
    "products": ("Products", "product"),
    "post": ("Posts", "post"),
    "posts": ("Posts", "post"),
    "blog": ("Posts", "post"),
    "article": ("Articles", "news"),
    "articles": ("Articles", "news"),
    "news": ("News", "news"),
    "profile": ("Profiles", "profile"),
    "profiles": ("Profiles", "profile"),
    "user": ("Profiles", "profile"),
    "users": ("Profiles", "profile"),
    "player": ("Players", "player"),
    "players": ("Players", "player"),
    "item": ("Items", "item"),
    "items": ("Items", "item"),
}
_COLLECTION_SEGMENTS = {
    "all",
    "search",
    "category",
    "categories",
    "collections",
    "trending",
    "top",
    "new",
    "latest",
    "popular",
}
_OPTIONAL_SLUG_PREFIXES = {
    "game",
    "games",
    "product",
    "products",
    "post",
    "posts",
    "article",
    "articles",
    "news",
}


def _dynamic_placeholder(
    value: str,
    *,
    prefix: str | None = None,
    first_after_prefix: bool = False,
) -> str:
    """Semantic placeholder for dynamic route pieces.

    Repeated content URLs commonly use an entity id/slug immediately after a
    content prefix, then optional human-readable slug pieces after that. Keeping
    the first slot as ``:id`` makes one route family stable across numeric ids,
    non-English slugs, and older surface-family payloads.
    """
    if first_after_prefix and prefix in _DETAIL_PREFIXES:
        return ":id"
    return ":id" if value.isdigit() else ":param"


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


@dataclass
class SurfaceFamily:
    """Repeated dynamic-content route family visible on a state surface."""

    pattern: str
    family_id: str
    label: str
    kind: str
    item_ids: list[str]
    sample_labels: list[str]
    sample_urls: list[str]

    @property
    def discovered_count(self) -> int:
        return len(self.item_ids)


@dataclass(frozen=True)
class UrlFamily:
    pattern: str
    family_id: str
    label: str
    kind: str


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
    active_prefix: str | None = None
    dynamic_index_after_prefix = 0
    for column in zip(*segments, strict=True):
        if len(set(column)) == 1:
            value = column[0]
            pattern_segments.append(value)
            if value not in {":id", "#"}:
                shared_literals += 1
            if value.lower() in _DETAIL_PREFIXES:
                active_prefix = value.lower()
                dynamic_index_after_prefix = 0
        else:
            first_after_prefix = active_prefix is not None and dynamic_index_after_prefix == 0
            pattern_segments.append(
                ":id"
                if all(value.isdigit() for value in column)
                else _dynamic_placeholder(
                    column[0],
                    prefix=active_prefix,
                    first_after_prefix=first_after_prefix,
                )
            )
            if active_prefix is not None:
                dynamic_index_after_prefix += 1
            varied = True
    if shared_literals == 0:
        return None

    query_rows = [dict(parse_qsl(part.query, keep_blank_values=True)) for part in normalized]
    query_keys = sorted(query_rows[0]) if query_rows else []
    pattern_query: list[tuple[str, str]] = []
    for key in query_keys:
        values = {row.get(key, "") for row in query_rows}
        if len(values) == 1:
            pattern_query.append((key, next(iter(values))))
        else:
            varied = True
            pattern_query.append((key, ":id" if all(value for value in values) else ":param"))
    if not varied:
        return None

    first = normalized[0]
    if (
        len(pattern_segments) >= 2
        and pattern_segments[-1] == ":id"
        and pattern_segments[-2].lower() in _OPTIONAL_SLUG_PREFIXES
    ):
        pattern_segments.append(":param")
    path = "/" + "/".join(pattern_segments) if pattern_segments else "/"
    return urlunsplit((first.scheme, first.netloc, path, urlencode(pattern_query, safe=":"), ""))


def _family_display(pattern: str, items: list[Interactable]) -> tuple[str, str, str]:
    family_id = hashlib.sha1(f"route-family|{pattern}".encode()).hexdigest()[:12]
    contexts = [item.context_label for item in items if item.context_label]
    if contexts and len(set(contexts)) == 1:
        label = contexts[0] or "Items"
    else:
        segments = [
            segment
            for segment in urlsplit(pattern).path.split("/")
            if segment and not segment.startswith(":")
        ]
        label = (segments[-1] if segments else "items").replace("-", " ").title()
    lowered = label.lower()
    kinds = (
        ("game", "Games"),
        ("video", "Videos"),
        ("product", "Products"),
        ("news", "News"),
        ("article", "Articles"),
        ("post", "Posts"),
        ("player", "Players"),
        ("profile", "Profiles"),
        ("user", "Profiles"),
        ("item", "Items"),
        ("card", "Cards"),
    )
    kind = next((kind for kind, _ in kinds if kind in lowered or kind in pattern.lower()), "items")
    if not contexts:
        canonical = next((name for needle, name in kinds if needle == kind), None)
        label = canonical or (label if label.endswith("s") else f"{label}s")
    return family_id, label, kind


def _family_id(pattern: str) -> str:
    return hashlib.sha1(f"route-family|{pattern}".encode()).hexdigest()[:12]


def _parameterized_path(segments: list[str], prefix_index: int) -> str | None:
    if prefix_index + 1 >= len(segments):
        return None
    first_dynamic = segments[prefix_index + 1].lower()
    if first_dynamic in _COLLECTION_SEGMENTS:
        return None
    prefix = segments[prefix_index].lower()
    pattern_segments = []
    for index, segment in enumerate(segments):
        if index <= prefix_index:
            pattern_segments.append(segment)
        else:
            pattern_segments.append(
                _dynamic_placeholder(
                    segment,
                    prefix=prefix,
                    first_after_prefix=index == prefix_index + 1,
                )
            )
    if (
        len(segments) == prefix_index + 2
        and pattern_segments[-1] == ":id"
        and prefix in _OPTIONAL_SLUG_PREFIXES
    ):
        pattern_segments.append(":param")
    return "/" + "/".join(pattern_segments)


def infer_url_family(url: str) -> UrlFamily | None:
    """Infer a dynamic detail-page family from a retained state's URL.

    This catches sampled pages that were reached from different surfaces or
    whose labels are non-English, while avoiding broad static pages such as
    /docs, /checkout, or /games/all.
    """
    parts = urlsplit(normalize_url(url))
    if parts.scheme not in {"http", "https", "file"}:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]

    if parts.netloc.endswith("youtube.com") and parts.path == "/watch":
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if query.get("v"):
            pattern = urlunsplit((parts.scheme, parts.netloc, "/watch", "v=:id", ""))
            return UrlFamily(pattern, _family_id(pattern), "Videos", "video")

    for index, segment in enumerate(segments):
        key = segment.lower()
        if key not in _DETAIL_PREFIXES:
            continue
        path = _parameterized_path(segments, index)
        if path is None:
            continue
        label, kind = _DETAIL_PREFIXES[key]
        pattern = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        return UrlFamily(pattern, _family_id(pattern), label, kind)

    return None


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


def _content_family_bucket(item: Interactable) -> str | None:
    href = urlsplit(item.href or "")
    path = href.path
    if href.scheme == "file":
        parts = [part for part in path.split("/") if part]
        path = "/".join(parts[-2:])
    haystack = " ".join(
        filter(None, [item.label, item.context_label, path])
    )
    match = _CONTENT_FAMILY_HINT.search(haystack)
    if not match:
        return None
    value = match.group(1).lower()
    if value.endswith("s"):
        value = value[:-1]
    if value == "blog":
        return "post"
    if value == "article":
        return "news"
    return value


def detect_surface_families(items: list[Interactable]) -> list[SurfaceFamily]:
    """Find repeated dynamic route families across all visible surface links.

    Unlike ``collapse_siblings``, this is not selector-cohort limited. It lets
    the explorer keep family/group context even when only one representative is
    sampled or when skipped links never become states.
    """
    cohorts: dict[tuple, list[Interactable]] = {}
    for item in items:
        shape = _href_shape(item)
        if shape is None:
            continue
        bucket = _content_family_bucket(item)
        if bucket is None:
            continue
        cohorts.setdefault((*shape, bucket), []).append(item)

    families: list[SurfaceFamily] = []
    seen_patterns: set[str] = set()
    for cohort in cohorts.values():
        pattern = _infer_family_pattern(cohort)
        if pattern is None or pattern in seen_patterns:
            continue
        family_id, family_label, family_kind = _family_display(pattern, cohort)
        for item in cohort:
            item.group_id = family_id
        families.append(
            SurfaceFamily(
                pattern=pattern,
                family_id=family_id,
                label=family_label,
                kind=family_kind,
                item_ids=[item.item_id or item.selector for item in cohort],
                sample_labels=[item.label for item in cohort[:8]],
                sample_urls=[item.href or "" for item in cohort[:8] if item.href],
            )
        )
        seen_patterns.add(pattern)
    return families


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
        bucket = _content_family_bucket(item)
        if bucket is None:
            continue
        selector_key = (item.tag, item.role or "", strip_positional_selector(item.selector))
        selector_cohorts.setdefault((*selector_key, *shape, bucket), []).append(item)

    family_by_item: dict[
        str, tuple[str, int, list[str], str, str, str]
    ] = {}
    for cohort in selector_cohorts.values():
        pattern = _infer_family_pattern(cohort)
        if pattern is None:
            continue
        group_id, family_label, family_kind = _family_display(pattern, cohort)
        labels = [item.label for item in cohort]
        for item in cohort:
            item.group_id = group_id
            family_by_item[item.item_id or item.selector] = (
                pattern,
                len(cohort),
                labels,
                group_id,
                family_label,
                family_kind,
            )

    groups: dict[tuple, ActionCandidate] = {}
    candidates: list[ActionCandidate] = []
    family_seen: set[str] = set()
    for item in items:
        family = family_by_item.get(item.item_id or item.selector)
        if family is not None:
            pattern, family_size, labels, family_id, family_label, family_kind = family
            # Keep members as separate candidates so the explorer can observe
            # and compare a bounded number of examples. The first candidate's
            # count describes the full discovered cohort in graph output.
            candidates.append(
                ActionCandidate(
                    interactable=item,
                    collapsed_count=family_size if pattern not in family_seen else 1,
                    grouped_labels=labels if pattern not in family_seen else [item.label],
                    family_pattern=pattern,
                    family_id=family_id,
                    family_label=family_label,
                    family_kind=family_kind,
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
    if item.region == "aside" and item.kind in {"link", "menuitem"}:
        score += _SCORE_ASIDE_NAV
    if item.region == "footer":
        score += _SCORE_FOOTER

    if item.href and not item.href.startswith("javascript:"):
        score += (
            _SCORE_VISITED_URL if normalize_url(item.href) in visited_urls else _SCORE_NEW_URL
        )

    return score
