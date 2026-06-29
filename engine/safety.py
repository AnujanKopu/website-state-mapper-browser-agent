"""Rule-based safety policy: decides which actions the agent may perform.

The agent's job is to *map* a product, never to mutate it. Anything that
could pay, delete, send, publish, accept, upload, or end the session is
denied and surfaced on the state as a risky/terminal boundary -- which is
itself a product insight, not a failure.

Rules are deliberately conservative; an LLM safety judge for gray areas
arrives in M2 and can only loosen decisions the rules call uncertain,
never override a hard deny.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from engine.schemas import Interactable


class SafetyCategory(StrEnum):
    EXTERNAL = "external"
    DOWNLOAD = "download"
    CONTACT_PROTOCOL = "contact_protocol"
    SESSION = "session"
    PAYMENT = "payment"
    DESTRUCTIVE = "destructive"
    COMMUNICATION = "communication"
    PUBLISH = "publish"
    LEGAL = "legal"
    UPLOAD = "upload"
    FORM_SUBMIT = "form_submit"


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    category: SafetyCategory | None = None
    reason: str = ""

    @staticmethod
    def allow() -> SafetyDecision:
        return SafetyDecision(allowed=True)

    @staticmethod
    def deny(category: SafetyCategory, reason: str) -> SafetyDecision:
        return SafetyDecision(allowed=False, category=category, reason=reason)


# Order matters: the first matching category names the denial.
_TEXT_RULES: list[tuple[SafetyCategory, re.Pattern[str]]] = [
    (
        SafetyCategory.SESSION,
        re.compile(r"\b(log\s?-?out|sign\s?-?out)\b", re.I),
    ),
    (
        SafetyCategory.PAYMENT,
        re.compile(
            r"\b(pay(\s+now)?|buy(\s+now)?|purchase|subscribe|place\s+order"
            r"|complete\s+(order|purchase|payment)|confirm\s+(order|payment)"
            r"|add\s+to\s+(cart|bag)|start\s+(my\s+)?subscription|renew)\b",
            re.I,
        ),
    ),
    (
        SafetyCategory.DESTRUCTIVE,
        re.compile(
            r"\b(delete|remove|destroy|erase|deactivate|disable|revoke|unsubscribe"
            r"|reset|cancel\s+(account|subscription|plan)|close\s+(my\s+)?account"
            r"|clear\s+(all|history))\b",
            re.I,
        ),
    ),
    (
        SafetyCategory.COMMUNICATION,
        re.compile(r"\b(send|invite|share|forward|reply|broadcast)\b", re.I),
    ),
    (
        SafetyCategory.PUBLISH,
        re.compile(r"\b(publish|post\s+(comment|reply)|submit\s+(review|comment)|tweet)\b", re.I),
    ),
    (
        SafetyCategory.LEGAL,
        re.compile(r"\b(accept|agree|consent|acknowledge)\b", re.I),
    ),
    (
        SafetyCategory.UPLOAD,
        re.compile(r"\b(upload|attach|choose\s+file|import)\b", re.I),
    ),
]

_HREF_SESSION = re.compile(r"(log-?out|sign-?out)", re.I)
_DOWNLOAD_EXTENSIONS = (
    ".pdf", ".zip", ".tar.gz", ".tgz", ".dmg", ".exe", ".msi", ".csv", ".xlsx", ".pkg",
)


def origin_of(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


def _canonical_web_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_same_origin(url: str, base_url: str) -> bool:
    """Safe same-site scope check for the mapper.

    For browser-app mapping, a canonical redirect such as youtube.com ->
    www.youtube.com, or http -> https on the same host, should stay in scope.
    All file:// URLs count as one origin so local fixture sites behave like a
    single site. Other subdomains remain out of scope.
    """
    scheme, host = origin_of(url)
    base_scheme, base_host = origin_of(base_url)
    if scheme == base_scheme == "file":
        return True
    if scheme in {"http", "https"} and base_scheme in {"http", "https"}:
        return _canonical_web_host(host) == _canonical_web_host(base_host)
    return (scheme, host) == (base_scheme, base_host)


def evaluate_action(item: Interactable, *, base_url: str) -> SafetyDecision:
    """Decide whether clicking this element is safe for a mapping agent."""
    href = item.href or ""

    if item.download:
        return SafetyDecision.deny(SafetyCategory.DOWNLOAD, "download attribute")

    if href.startswith(("mailto:", "tel:", "sms:")):
        return SafetyDecision.deny(
            SafetyCategory.CONTACT_PROTOCOL, f"contact protocol link: {href.split(':')[0]}:"
        )
    if href and not href.startswith("javascript:"):
        href_path = urlsplit(href).path.lower()
        if href_path.endswith(_DOWNLOAD_EXTENSIONS):
            return SafetyDecision.deny(SafetyCategory.DOWNLOAD, f"file download: {href_path}")
        if href.startswith(("http://", "https://")) and not is_same_origin(href, base_url):
            return SafetyDecision.deny(
                SafetyCategory.EXTERNAL, f"external origin: {urlsplit(href).netloc}"
            )
        if _HREF_SESSION.search(href_path):
            return SafetyDecision.deny(SafetyCategory.SESSION, "logout link (ends the session)")

    haystack = " ".join(
        filter(
            None,
            [
                item.text,
                item.aria_label,
                item.associated_label,
                item.title,
                item.icon_label,
                item.context_label,
            ],
        )
    )
    if re.search(r"\b(download|export\s+(csv|xlsx|pdf))\b", haystack, re.I):
        return SafetyDecision.deny(SafetyCategory.DOWNLOAD, "download control")
    for category, pattern in _TEXT_RULES:
        match = pattern.search(haystack)
        if match:
            return SafetyDecision.deny(category, f"matched {category.value!r}: {match.group(0)!r}")

    # No form is submitted until safe synthetic-fill support lands (M2+).
    # Buttons inside forms default to type=submit, so deny them wholesale.
    if item.in_form and (
        item.tag == "button"
        or (
            item.tag == "input"
            and (
                (item.input_type or "").lower() in {"submit", "button", "image"}
                or (item.input_type is None and bool(item.text))
            )
        )
    ):
        return SafetyDecision.deny(
            SafetyCategory.FORM_SUBMIT, "form submission (deferred until safe form-fill support)"
        )

    return SafetyDecision.allow()
