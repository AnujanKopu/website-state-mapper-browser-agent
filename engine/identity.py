"""State identity primitives: URL normalization and content hashing.

M0 ships the URL + visible-text layers of the fingerprint. The DOM skeleton
hash, simhash, and screenshot pHash layers land in M1 -- they extend
`state_fingerprint` without changing its callers.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def text_hash(visible_text: str) -> str:
    """Hash of visible text, insensitive to whitespace and casing."""
    collapsed = _WHITESPACE.sub(" ", visible_text).strip().lower()
    return hashlib.sha1(collapsed.encode("utf-8")).hexdigest()


def state_fingerprint(url_normalized: str, text_digest: str) -> str:
    """Deterministic identity for a state, used for graph deduplication."""
    return hashlib.sha1(f"{url_normalized}|{text_digest}".encode()).hexdigest()[:16]
