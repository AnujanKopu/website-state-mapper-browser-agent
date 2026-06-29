"""Private client for the one-run hosted worker supervisor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from engine.schemas import Credentials, RunConfig


class SupervisorClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"x-flowstate-supervisor-token": token} if token else {}

    async def stream_run(
        self,
        *,
        run_id: str,
        url: str,
        config: RunConfig,
        credentials: Credentials | None,
    ) -> AsyncIterator[dict]:
        payload = {
            "url": url,
            "config": config.model_dump(mode="json"),
            "credentials": credentials.model_dump(mode="json") if credentials else None,
        }
        timeout = httpx.Timeout(None, connect=15.0)
        async with (
            httpx.AsyncClient(timeout=timeout, headers=self._headers) as client,
            client.stream(
                "POST",
                f"{self._base_url}/private/runs/{run_id}/start",
                json=payload,
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)

    async def command(
        self,
        run_id: str,
        command: str,
        credentials: Credentials | None = None,
    ) -> None:
        payload = {
            "type": command,
            "credentials": credentials.model_dump(mode="json") if credentials else None,
        }
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            response = await client.post(
                f"{self._base_url}/private/runs/{run_id}/command", json=payload
            )
            response.raise_for_status()

    async def cleanup(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            response = await client.delete(
                f"{self._base_url}/private/runs/{run_id}"
            )
            response.raise_for_status()
