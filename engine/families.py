"""Run-scoped, content-agnostic dynamic URL family inference.

URL shape is only candidate evidence. A family becomes authoritative after
multiple sampled destinations demonstrate compatible rendered structure.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

from engine.identity import strip_positional_selector
from engine.schemas import Interactable, Observation

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "ref_src",
    "s_kwcid",
}
_PLACEHOLDER = re.compile(r"^(?::(?:id|param|slug|value|optional)|\{[^}]+\})$")
_SKELETON_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith("utm_")


@dataclass(frozen=True)
class UrlTokens:
    url: str
    scheme: str
    netloc: str
    segments: tuple[str, ...]
    query: tuple[tuple[str, str], ...]

    @property
    def origin(self) -> tuple[str, str]:
        return self.scheme, self.netloc

    @property
    def query_keys(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.query)


def tokenize_url(url: str, *, base_url: str | None = None) -> UrlTokens | None:
    """Return stable RFC-3986 components without discarding dynamic values."""
    absolute = urljoin(base_url or url, url)
    parts = urlsplit(absolute)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https", "file"}:
        return None
    netloc = parts.netloc.lower()
    segments = tuple(unquote(value) for value in parts.path.split("/") if value)
    query = tuple(
        sorted(
            (unquote(name), unquote(value))
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(name)
        )
    )
    path = "/" + "/".join(quote(value, safe=":@") for value in segments) if segments else "/"
    query_text = "&".join(
        f"{quote(name, safe=':@')}={quote(value, safe=':@')}" for name, value in query
    )
    normalized = urlunsplit((scheme, netloc, path, query_text, ""))
    return UrlTokens(normalized, scheme, netloc, segments, query)


@dataclass(frozen=True)
class LinkEvidence:
    url: str
    source_key: str
    source_structure: str
    item_id: str
    selector_shape: str
    container_key: str
    context_label: str
    label: str
    fold: int
    peripheral: bool
    order: int

    @property
    def support_key(self) -> str:
        return "|".join((self.source_key, self.container_key, self.selector_shape))


@dataclass(frozen=True)
class StructureSignature:
    url: str
    origin: tuple[str, str]
    skeleton_shingles: Counter[str]
    affordances: frozenset[str]
    auth_context: str
    signal_profile: tuple[bool, bool, bool, bool]


@dataclass
class FamilyCandidate:
    pattern: str
    family_id: str
    label: str
    urls: list[str]
    evidences: list[LinkEvidence]
    dynamic_slots: list[str]
    status: str = "provisional"
    sample_targets: list[str] = field(default_factory=list)
    samples: dict[str, StructureSignature] = field(default_factory=dict)
    deferred_urls: set[str] = field(default_factory=set)
    skipped_urls: set[str] = field(default_factory=set)

    @property
    def support_sources(self) -> list[str]:
        return sorted({item.support_key for item in self.evidences if not item.peripheral})[:16]

    def payload(self) -> dict:
        sample_labels = list(dict.fromkeys(item.label for item in self.evidences if item.label))[:8]
        return {
            "id": self.family_id,
            "label": self.label,
            "kind": "items",
            "pattern": self.pattern,
            "label_source": "heuristic",
            "confidence": 0.92 if self.status == "confirmed" else 0.72,
            "status": self.status,
            "discovered_count": len(self.urls),
            "checked_count": len(self.samples),
            "represented_count": 0,
            "skipped_count": len(self.skipped_urls),
            "support_sources": self.support_sources,
            "variant_count": 0,
            "dynamic_slots": list(self.dynamic_slots),
            "sample_labels": sample_labels,
            "sample_urls": list(self.samples)[:8],
        }


def _family_id(pattern: str) -> str:
    return hashlib.sha1(f"route-family|{pattern}".encode()).hexdigest()[:12]


def _render_pattern(
    first: UrlTokens,
    path: list[str],
    query: list[tuple[str, str]],
) -> str:
    rendered_path = (
        "/"
        + "/".join(value if value.startswith(":") else quote(value, safe=":@") for value in path)
        if path
        else "/"
    )
    rendered_query = "&".join(
        f"{quote(key, safe=':@')}={value if value.startswith(':') else quote(value, safe=':@')}"
        for key, value in query
    )
    return urlunsplit((first.scheme, first.netloc, rendered_path, rendered_query, ""))


def infer_template(urls: list[str], *, optional_tail: bool = False) -> tuple[str, list[str]] | None:
    """Anti-unify a same-origin cohort into literals and positional slots."""
    tokens = [tokenize_url(url) for url in dict.fromkeys(urls)]
    if len(tokens) < 2 or any(item is None for item in tokens):
        return None
    rows = [item for item in tokens if item is not None]
    first = rows[0]
    if any(row.origin != first.origin or row.query_keys != first.query_keys for row in rows):
        return None

    lengths = {len(row.segments) for row in rows}
    if len(lengths) > 1 and (not optional_tail or max(lengths) - min(lengths) != 1):
        return None
    width = min(lengths)
    path: list[str] = []
    slots: list[str] = []
    varied = False
    for index in range(width):
        values = {row.segments[index] for row in rows}
        if len(values) == 1:
            path.append(next(iter(values)))
        else:
            path.append(":param")
            slots.append(f"path:{index}")
            varied = True
    if len(lengths) == 2:
        tail = [row.segments[-1] for row in rows if len(row.segments) == max(lengths)]
        if len(set(tail)) < 2:
            return None
        path.append(":optional")
        slots.append(f"path:{width}?")
        varied = True

    query_rows = [dict(row.query) for row in rows]
    query: list[tuple[str, str]] = []
    for key in first.query_keys:
        values = {row[key] for row in query_rows}
        if len(values) == 1:
            query.append((key, next(iter(values))))
        else:
            query.append((key, ":param"))
            slots.append(f"query:{key}")
            varied = True
    if not varied:
        return None
    return _render_pattern(first, path, query), slots


def _pattern_parts(pattern: str) -> tuple[UrlTokens, bool] | None:
    parts = urlsplit(pattern)
    optional = parts.path.rstrip("/").endswith("/:optional")
    parsed = tokenize_url(pattern)
    return (parsed, optional) if parsed is not None else None


def matches_template(url: str, pattern: str) -> bool:
    parsed = tokenize_url(url)
    pattern_parts = _pattern_parts(pattern)
    if parsed is None or pattern_parts is None:
        return False
    template, optional = pattern_parts
    if parsed.origin != template.origin or parsed.query_keys != template.query_keys:
        return False
    required = len(template.segments) - (1 if optional else 0)
    if len(parsed.segments) not in ({required, required + 1} if optional else {required}):
        return False
    for index, expected in enumerate(template.segments[:required]):
        if not _PLACEHOLDER.match(expected) and parsed.segments[index] != expected:
            return False
    actual_query = dict(parsed.query)
    for key, expected in template.query:
        if not _PLACEHOLDER.match(expected) and actual_query.get(key) != expected:
            return False
    return True


def _literal_count(pattern: str) -> int:
    parsed = tokenize_url(pattern)
    if parsed is None:
        return 0
    return sum(not _PLACEHOLDER.match(value) for value in parsed.segments) + len(parsed.query_keys)


def _dynamic_values(url: str, pattern: str) -> list[str]:
    parsed = tokenize_url(url)
    template_parts = _pattern_parts(pattern)
    if parsed is None or template_parts is None:
        return []
    template, optional = template_parts
    values = [
        parsed.segments[index]
        for index, expected in enumerate(template.segments[: len(parsed.segments)])
        if _PLACEHOLDER.match(expected)
    ]
    actual_query = dict(parsed.query)
    values.extend(
        actual_query.get(key, "")
        for key, expected in template.query
        if _PLACEHOLDER.match(expected)
    )
    if optional and len(parsed.segments) < len(template.segments):
        values.append("")
    return values


def _lexical_shape(value: str) -> str:
    if not value:
        return "empty"
    if value.isdigit():
        kind = "digits"
    elif value.isascii() and value.isalnum():
        kind = "alnum"
    elif value.isascii():
        kind = "slug"
    else:
        kind = "unicode"
    return f"{kind}:{min(len(value) // 4, 8)}"


def _sample_distance(left: LinkEvidence, right: LinkEvidence, pattern: str) -> float:
    left_values = _dynamic_values(left.url, pattern)
    right_values = _dynamic_values(right.url, pattern)
    width = max(len(left_values), len(right_values), 1)
    value_distance = (
        sum(
            1.0 if a != b else 0.0
            for a, b in zip(
                left_values + [""] * width,
                right_values + [""] * width,
                strict=False,
            )
        )
        / width
    )
    shape_distance = (
        sum(
            1.0 if _lexical_shape(a) != _lexical_shape(b) else 0.0
            for a, b in zip(
                left_values + [""] * width,
                right_values + [""] * width,
                strict=False,
            )
        )
        / width
    )
    context_distance = (
        sum(
            (
                left.selector_shape != right.selector_shape,
                left.container_key != right.container_key,
                left.source_structure != right.source_structure,
                left.fold != right.fold,
            )
        )
        / 4
    )
    return value_distance * 0.45 + shape_distance * 0.25 + context_distance * 0.30


def _pick_diverse_samples(
    urls: list[str], evidences: list[LinkEvidence], pattern: str, count: int
) -> list[str]:
    by_url: dict[str, LinkEvidence] = {}
    for evidence in sorted(evidences, key=lambda item: item.order):
        by_url.setdefault(evidence.url, evidence)
    available = [url for url in urls if url in by_url]
    if not available:
        return []
    selected = [available[0]]
    while len(selected) < min(count, len(available)):
        remaining = [url for url in available if url not in selected]
        chosen = max(
            remaining,
            key=lambda url: (
                min(
                    _sample_distance(by_url[url], by_url[current], pattern) for current in selected
                ),
                -by_url[url].order,
                url,
            ),
        )
        selected.append(chosen)
    return selected


def _skeleton_shingles(skeleton: str) -> Counter[str]:
    tokens = [item.lower() for item in _SKELETON_TOKEN.findall(skeleton)]
    if len(tokens) < 3:
        return Counter(tokens)
    return Counter("/".join(tokens[index : index + 3]) for index in range(len(tokens) - 2))


def structure_signature(observation: Observation) -> StructureSignature:
    signals = observation.snapshot.signals
    affordances = frozenset(
        "|".join(
            (
                item.tag,
                item.role or "",
                item.kind or "",
                item.region or "",
                "form" if item.in_form else "",
                "modal" if item.in_modal else "",
                item.input_type or "",
            )
        )
        for item in observation.interactables
    )
    parsed = tokenize_url(observation.snapshot.url)
    return StructureSignature(
        url=parsed.url if parsed else observation.snapshot.url,
        origin=parsed.origin if parsed else ("", ""),
        skeleton_shingles=_skeleton_shingles(observation.snapshot.dom_skeleton),
        affordances=affordances,
        auth_context=observation.auth_context.value,
        signal_profile=(
            signals.modal_open,
            signals.password_fields > 0,
            signals.payment_fields > 0,
            signals.form_count > 0,
        ),
    )


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def structures_compatible(left: StructureSignature, right: StructureSignature) -> bool:
    if left.origin != right.origin or left.auth_context != right.auth_context:
        return False
    if left.signal_profile != right.signal_profile:
        return False
    return (
        _cosine(left.skeleton_shingles, right.skeleton_shingles) >= 0.72
        and _jaccard(left.affordances, right.affordances) >= 0.5
    )


class FamilyRegistry:
    """Accumulates URL evidence and validates promoted cohorts for one run."""

    def __init__(
        self,
        *,
        min_support: int = 5,
        strong_support: int = 3,
        sample_cap: int = 3,
        validation_cap: int = 5,
    ) -> None:
        self.min_support = min_support
        self.strong_support = strong_support
        self.sample_cap = sample_cap
        self.validation_cap = validation_cap
        self._evidence: dict[str, list[LinkEvidence]] = defaultdict(list)
        self._tokens: dict[str, UrlTokens] = {}
        self._candidates: dict[str, FamilyCandidate] = {}
        self._url_to_pattern: dict[str, str] = {}
        self._order = 0

    @property
    def candidates(self) -> dict[str, FamilyCandidate]:
        return self._candidates

    def observe_surface(
        self,
        *,
        source_key: str,
        source_structure: str,
        base_url: str,
        items: list[Interactable],
    ) -> list[FamilyCandidate]:
        for item in items:
            if not item.href:
                continue
            parsed = tokenize_url(item.href, base_url=base_url)
            if parsed is None:
                continue
            peripheral = item.in_nav or item.region in {"nav", "header", "footer"}
            evidence = LinkEvidence(
                url=parsed.url,
                source_key=source_key,
                source_structure=source_structure,
                item_id=item.item_id or item.selector,
                selector_shape=strip_positional_selector(item.selector),
                container_key=item.container_key or "",
                context_label=(item.context_label or "").strip(),
                label=item.label,
                fold=item.fold,
                peripheral=peripheral,
                order=self._order,
            )
            self._order += 1
            key = (evidence.source_key, evidence.item_id, evidence.url)
            if any(
                (old.source_key, old.item_id, old.url) == key for old in self._evidence[parsed.url]
            ):
                continue
            self._evidence[parsed.url].append(evidence)
            self._tokens[parsed.url] = parsed
        self._rebuild_candidates()
        visible_urls = {
            parsed.url
            for item in items
            if item.href and (parsed := tokenize_url(item.href, base_url=base_url)) is not None
        }
        return [
            candidate
            for candidate in self._candidates.values()
            if candidate.status != "rejected" and visible_urls.intersection(candidate.urls)
        ]

    def _rebuild_candidates(self) -> None:
        usable = {
            url
            for url, evidences in self._evidence.items()
            if any(not item.peripheral for item in evidences)
        }
        anchored: dict[tuple, set[str]] = defaultdict(set)
        optional_anchored: dict[tuple, set[str]] = defaultdict(set)
        query_groups: dict[tuple, set[str]] = defaultdict(set)
        strong_groups: dict[tuple, set[str]] = defaultdict(set)
        for url in usable:
            tokens = self._tokens[url]
            for index, value in enumerate(tokens.segments):
                anchored[
                    (tokens.origin, tokens.query_keys, len(tokens.segments), index, value)
                ].add(url)
                optional_anchored[(tokens.origin, tokens.query_keys, index, value)].add(url)
            if tokens.query_keys:
                query_groups[(tokens.origin, tokens.segments, tokens.query_keys)].add(url)
            for evidence in self._evidence[url]:
                if evidence.peripheral:
                    continue
                strong_groups[
                    (
                        evidence.support_key,
                        tokens.origin,
                        tokens.query_keys,
                        len(tokens.segments),
                    )
                ].add(url)

        raw_groups: list[tuple[set[str], bool]] = []
        raw_groups.extend((urls, False) for urls in anchored.values())
        raw_groups.extend((urls, False) for urls in query_groups.values())
        raw_groups.extend((urls, False) for urls in strong_groups.values())
        raw_groups.extend(
            (urls, True)
            for urls in optional_anchored.values()
            if len({len(self._tokens[url].segments) for url in urls}) == 2
        )

        inferred: dict[str, tuple[set[str], list[str]]] = {}
        for urls, optional in raw_groups:
            if len(urls) < 2:
                continue
            result = infer_template(sorted(urls), optional_tail=optional)
            if result is None:
                continue
            pattern, slots = result
            matched = {url for url in usable if matches_template(url, pattern)}
            if len(matched) < 2:
                continue
            current = inferred.setdefault(pattern, (set(), slots))
            current[0].update(matched)

        promoted: dict[str, tuple[list[str], list[LinkEvidence], list[str]]] = {}
        for pattern, (urls, slots) in inferred.items():
            evidences = [
                item for url in urls for item in self._evidence[url] if not item.peripheral
            ]
            by_source: dict[str, set[str]] = defaultdict(set)
            for item in evidences:
                by_source[item.support_key].add(item.url)
            if ":optional" in pattern:
                strong_count = max(
                    (
                        len(values)
                        for values in by_source.values()
                        if len({len(self._tokens[url].segments) for url in values}) == 2
                    ),
                    default=0,
                )
            else:
                strong_count = max((len(values) for values in by_source.values()), default=0)
            stable = _literal_count(pattern) > 0
            parsed_pattern = tokenize_url(pattern)
            bare = bool(
                parsed_pattern
                and len(parsed_pattern.segments) == 1
                and _PLACEHOLDER.match(parsed_pattern.segments[0])
            )
            strong = strong_count >= self.strong_support and (stable or bare)
            # ``file://`` fixture paths share long machine-directory prefixes
            # that are not application route evidence. Local fixtures still
            # promote through repeated-container support.
            weak = (
                len(urls) >= self.min_support
                and stable
                and bool(parsed_pattern)
                and parsed_pattern.scheme != "file"
            )
            dynamic_rows = [_dynamic_values(url, pattern) for url in urls]
            slot_count = max((len(row) for row in dynamic_rows), default=0)
            cardinality = max(
                (
                    len({row[index] for row in dynamic_rows if index < len(row)})
                    for index in range(slot_count)
                ),
                default=0,
            )
            if not (strong or weak) or cardinality < min(3, len(urls)):
                continue
            promoted[pattern] = (sorted(urls, key=self._first_order), evidences, slots)

        optional_patterns = [
            (pattern, set(values[0]))
            for pattern, values in promoted.items()
            if ":optional" in pattern
        ]
        promoted = {
            pattern: values
            for pattern, values in promoted.items()
            if ":optional" in pattern
            or not any(
                set(values[0]) < optional_urls
                for _optional_pattern, optional_urls in optional_patterns
            )
        }
        for pattern in list(self._candidates):
            if pattern not in promoted and self._candidates[pattern].status == "provisional":
                del self._candidates[pattern]

        for pattern, (urls, evidences, slots) in promoted.items():
            existing = self._candidates.get(pattern)
            if existing is None:
                label = self._label_for(pattern, evidences)
                existing = FamilyCandidate(
                    pattern, _family_id(pattern), label, urls, evidences, slots
                )
                self._candidates[pattern] = existing
            else:
                existing.urls = urls
                existing.evidences = evidences
                existing.dynamic_slots = slots
            target_count = min(self.sample_cap, len(urls))
            if len(existing.sample_targets) < target_count:
                existing.sample_targets = _pick_diverse_samples(
                    urls, evidences, pattern, target_count
                )

        self._url_to_pattern.clear()
        ranked = sorted(
            self._candidates.values(),
            key=lambda item: (
                0 if item.status == "confirmed" else 1,
                len(item.dynamic_slots),
                -_literal_count(item.pattern),
                -len(item.urls),
                item.pattern,
            ),
        )
        for candidate in ranked:
            if candidate.status == "rejected":
                continue
            for url in candidate.urls:
                self._url_to_pattern.setdefault(url, candidate.pattern)

    def _first_order(self, url: str) -> int:
        return min(item.order for item in self._evidence[url])

    @staticmethod
    def _label_for(pattern: str, evidences: list[LinkEvidence]) -> str:
        contexts = [item.context_label for item in evidences if item.context_label]
        if contexts and len({value.casefold() for value in contexts}) == 1:
            return contexts[0]
        parsed = tokenize_url(pattern)
        literals = [
            value
            for value in (parsed.segments if parsed else ())
            if not _PLACEHOLDER.match(value) and len(value) > 1
        ]
        value = literals[-1] if literals else "items"
        return value.replace("-", " ").replace("_", " ").title()

    def family_for_url(self, url: str, *, base_url: str | None = None) -> FamilyCandidate | None:
        parsed = tokenize_url(url, base_url=base_url)
        if parsed is None:
            return None
        pattern = self._url_to_pattern.get(parsed.url)
        return self._candidates.get(pattern) if pattern else None

    def should_sample(self, candidate: FamilyCandidate, url: str) -> bool:
        parsed = tokenize_url(url)
        return bool(
            parsed
            and parsed.url in candidate.sample_targets
            and parsed.url not in candidate.samples
        )

    def record_sample(
        self, candidate: FamilyCandidate, url: str, observation: Observation
    ) -> tuple[str, str]:
        """Record a fetched destination and return ``(old_status, new_status)``."""
        old_status = candidate.status
        parsed = tokenize_url(url)
        observed = tokenize_url(observation.snapshot.url)
        if (
            parsed is None
            or observed is None
            or not matches_template(parsed.url, candidate.pattern)
            or not matches_template(observed.url, candidate.pattern)
        ):
            candidate.status = "rejected"
            return old_status, candidate.status
        candidate.samples[parsed.url] = structure_signature(observation)
        samples = list(candidate.samples.values())
        compatible_pair = any(
            structures_compatible(samples[left], samples[right])
            for left in range(len(samples))
            for right in range(left + 1, len(samples))
        )
        if compatible_pair:
            candidate.status = "confirmed"
        elif len(samples) >= min(len(candidate.urls), len(candidate.sample_targets)):
            next_count = min(self.validation_cap, len(candidate.urls))
            if len(candidate.sample_targets) < next_count:
                candidate.sample_targets = _pick_diverse_samples(
                    candidate.urls,
                    candidate.evidences,
                    candidate.pattern,
                    min(len(candidate.sample_targets) + 1, next_count),
                )
            else:
                candidate.status = "rejected"
        return old_status, candidate.status

    def reject_unresolved(self) -> list[FamilyCandidate]:
        rejected = []
        for candidate in self._candidates.values():
            if candidate.status == "provisional":
                candidate.status = "rejected"
                rejected.append(candidate)
        return rejected

    def mark_deferred(self, candidate: FamilyCandidate, url: str) -> None:
        parsed = tokenize_url(url)
        if parsed:
            candidate.deferred_urls.add(parsed.url)

    def mark_skipped(self, candidate: FamilyCandidate, url: str) -> None:
        parsed = tokenize_url(url)
        if parsed:
            candidate.skipped_urls.add(parsed.url)

    def stats(self) -> dict[str, int]:
        candidates = list(self._candidates.values())
        return {
            "family_candidates_provisional": sum(
                item.status == "provisional" for item in candidates
            ),
            "family_candidates_confirmed": sum(item.status == "confirmed" for item in candidates),
            "family_candidates_rejected": sum(item.status == "rejected" for item in candidates),
            "family_urls_sampled": len({url for item in candidates for url in item.samples}),
            "family_urls_deferred": len({url for item in candidates for url in item.deferred_urls}),
            "family_urls_skipped": len({url for item in candidates for url in item.skipped_urls}),
        }
