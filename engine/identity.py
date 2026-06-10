"""State identity: URL normalization, content hashing, and the dedup match rule.

A state's identity is layered, cheapest signal first:

1. normalized URL          (tracking params and entity ids stripped)
2. modal-open flag         (a page and its modal are never the same state)
3. DOM skeleton hash       (visible structure only -- no text, lists truncated)
4. action signature        (set of positional-stripped interactable selectors)

Two observations with the same four layers are the same state. As a fuzzy
fallback, observations whose skeletons differ but whose visible-text simhash
and screenshot dHash are both near-identical (and whose action signatures
match) are merged too -- this absorbs minor structural noise such as rotating
embeds or ad iframes without conflating genuinely different UI states.

Design stance: states with identical affordances (same URL, same actionable
elements, same structure) are behaviorally equivalent in FSM terms, even if
body text differs. Text-only changes (timestamps, counters) never create new
states. LLMs are never part of identity.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image

from engine.schemas import Interactable, Observation

# --------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------

# Query parameters that carry tracking noise, never state.
_TRACKING_PARAMS = {
    "gclid",
    "fbclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "igshid",
    "s_kwcid",
}
_TRACKING_PREFIXES = ("utm_",)

# Path segments that are entity identifiers rather than distinct pages.
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_SEGMENT = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

_WHITESPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_NTH_OF_TYPE = re.compile(r":nth-of-type\(\d+\)")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def _normalize_segment(segment: str) -> str:
    if (
        _NUMERIC_SEGMENT.match(segment)
        or _UUID_SEGMENT.match(segment)
        or _HEX_SEGMENT.match(segment)
    ):
        return ":id"
    return segment


def normalize_url(url: str) -> str:
    """Canonicalize a URL so cosmetically different URLs map to one identity.

    - lowercases scheme and host
    - drops the fragment
    - drops tracking query params, sorts the rest
    - rewrites numeric / UUID / long-hex path segments to ``:id``
    - strips the trailing slash (except for the root path)
    """
    scheme, netloc, path, query, _fragment = urlsplit(url)

    kept_params = sorted(
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not _is_tracking_param(name)
    )

    segments = [_normalize_segment(s) for s in path.split("/") if s]
    normalized_path = "/" + "/".join(segments) if segments else "/"

    return urlunsplit(
        (scheme.lower(), netloc.lower(), normalized_path, urlencode(kept_params), "")
    )


# --------------------------------------------------------------------------
# Content hashing
# --------------------------------------------------------------------------


def _strip_dynamic(text: str) -> str:
    """Neutralize content that changes between visits without changing the
    state: digits cover timestamps, counters, prices-of-the-day, etc."""
    return _DIGITS.sub("0", text)


def text_hash(visible_text: str) -> str:
    """Exact hash of visible text, insensitive to whitespace, casing, digits."""
    collapsed = _WHITESPACE.sub(" ", _strip_dynamic(visible_text)).strip().lower()
    return hashlib.sha1(collapsed.encode("utf-8")).hexdigest()


def text_simhash(visible_text: str, bits: int = 64) -> int:
    """64-bit simhash over token 3-gram shingles of the visible text.

    Near-identical documents land within a small Hamming distance; used as
    a fuzzy identity signal, never as an exact key.
    """
    tokens = re.findall(r"[a-z0-9]+", _strip_dynamic(visible_text).lower())
    if not tokens:
        return 0
    shingles = (
        [" ".join(tokens[i : i + 3]) for i in range(len(tokens) - 2)]
        if len(tokens) >= 3
        else [" ".join(tokens)]
    )
    weights = [0] * bits
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            weights[i] += 1 if (h >> i) & 1 else -1
    return sum(1 << i for i in range(bits) if weights[i] > 0)


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def dom_skeleton_hash(skeleton: str) -> str:
    """Hash of the rendered DOM skeleton (structure only, produced in-page)."""
    return hashlib.sha1(skeleton.encode("utf-8")).hexdigest()


def strip_positional_selector(selector: str) -> str:
    """Remove :nth-of-type() indices so structurally identical siblings
    (card #3 vs card #7) share the same selector shape."""
    return _NTH_OF_TYPE.sub("", selector)


def action_signature(interactables: list[Interactable]) -> str:
    """Hash of the *set of affordances* a state offers.

    Built from positional-stripped selectors + tags, so sibling counts and
    rotating labels don't change the signature, but appearing/disappearing
    controls (modal buttons, revealed dropdown links) do.
    """
    shapes = sorted({f"{i.tag}|{strip_positional_selector(i.selector)}" for i in interactables})
    return hashlib.sha1("\n".join(shapes).encode("utf-8")).hexdigest()


def screenshot_dhash(png_bytes: bytes) -> int:
    """64-bit difference hash of a screenshot (gradient direction per cell).

    Robust to scaling and small rendering noise; cheap tiebreaker for the
    fuzzy identity layer.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L").resize((9, 8), Image.LANCZOS)
    pixels = img.tobytes()  # 8 rows x 9 cols of grayscale bytes
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def state_fingerprint(*components: str) -> str:
    """Deterministic identity string over the exact-match identity layers."""
    return hashlib.sha1("|".join(components).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Dedup index
# --------------------------------------------------------------------------

SIMHASH_MAX_HAMMING = 6
DHASH_MAX_HAMMING = 8


@dataclass(frozen=True)
class StateKey:
    """All identity signals for one observation."""

    url_normalized: str
    modal_open: bool
    skeleton_hash: str
    action_sig: str
    text_simhash: int
    screenshot_dhash: int


def key_for(observation: Observation) -> StateKey:
    return StateKey(
        url_normalized=observation.url_normalized,
        modal_open=observation.snapshot.signals.modal_open,
        skeleton_hash=observation.skeleton_hash,
        action_sig=observation.action_sig,
        text_simhash=observation.text_simhash,
        screenshot_dhash=observation.screenshot_dhash,
    )


@dataclass
class IdentityIndex:
    """Per-run dedup index implementing the layered match rule.

    Buckets by (normalized URL, modal flag); within a bucket:
    - exact:  same skeleton hash + same action signature
    - fuzzy:  same action signature, simhash and dHash both within thresholds
              (absorbs structural noise that text/pixels say is the same state)
    """

    _buckets: dict[tuple[str, bool], list[tuple[StateKey, str]]] = field(default_factory=dict)

    def find(self, key: StateKey) -> str | None:
        bucket = self._buckets.get((key.url_normalized, key.modal_open), [])
        for known, state_id in bucket:
            if known.action_sig != key.action_sig:
                continue
            if known.skeleton_hash == key.skeleton_hash:
                return state_id
            if (
                hamming_distance(known.text_simhash, key.text_simhash) <= SIMHASH_MAX_HAMMING
                and hamming_distance(known.screenshot_dhash, key.screenshot_dhash)
                <= DHASH_MAX_HAMMING
            ):
                return state_id
        return None

    def add(self, key: StateKey, state_id: str) -> None:
        self._buckets.setdefault((key.url_normalized, key.modal_open), []).append((key, state_id))
