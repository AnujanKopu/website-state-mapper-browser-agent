"""Network destination checks shared by the public API and hosted worker."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


def validate_public_http_url(url: str, *, resolve_host: bool = False) -> str:
    """Validate scheme/host and reject non-public literal addresses.

    DNS resolution is performed by ``is_public_destination`` at the worker
    boundary. Keeping this function synchronous makes it suitable for
    Pydantic request validation.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parts.hostname:
        raise ValueError("url must include a host")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError("url must target a public network address")
    return url


async def is_public_destination(url: str) -> bool:
    """Resolve all A/AAAA answers and require every answer to be public."""
    try:
        validate_public_http_url(url)
    except ValueError:
        return False
    parts = urlsplit(url)
    host = parts.hostname
    if host is None:
        return False
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            0,
            socket.SOCK_STREAM,
        )
    except OSError:
        return False
    addresses = {item[4][0].split("%", 1)[0] for item in results}
    return bool(addresses) and all(ipaddress.ip_address(value).is_global for value in addresses)
