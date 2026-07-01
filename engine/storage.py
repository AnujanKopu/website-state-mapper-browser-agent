"""Artifact storage for run outputs (screenshots, DOM snapshots).

`StorageBackend` is the seam for swapping local disk for S3/R2 later:
keys are storage-relative POSIX paths, and backends translate them to
their own addressing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class StorageBackend(Protocol):
    def save_bytes(self, key: str, data: bytes) -> str:
        """Persist binary data under `key`; returns the key."""
        ...

    def save_text(self, key: str, text: str) -> str:
        """Persist text data under `key`; returns the key."""
        ...


class LocalStorage:
    """Stores artifacts under a root directory on the local filesystem."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def save_bytes(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def save_text(self, key: str, text: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return key

    def path_for(self, key: str) -> Path:
        """Absolute path of a stored artifact (local backend only)."""
        return self._resolve(key)

    def _resolve(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"Storage key escapes root: {key!r}")
        return path


def screenshot_key(run_id: str, state_id: str, extension: str = "png") -> str:
    if extension not in {"png", "webp"}:
        raise ValueError(f"Unsupported screenshot extension: {extension!r}")
    return f"runs/{run_id}/screenshots/{state_id}.{extension}"


def dom_snapshot_key(run_id: str, state_id: str) -> str:
    return f"runs/{run_id}/dom/{state_id}.html"
