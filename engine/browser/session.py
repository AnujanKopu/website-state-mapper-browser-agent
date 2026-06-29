"""Playwright browser lifecycle with engine-wide guards.

Guards applied to every page:
- native dialogs (alert/confirm/prompt) are auto-dismissed so they never
  block the agent
- downloads are cancelled (the agent maps states; it never saves files)
"""

from __future__ import annotations

from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Download,
    Page,
    Request,
    Route,
    async_playwright,
)

from engine.network_policy import is_public_destination
from engine.safety import is_same_origin
from engine.schemas import BrowserConfig


class BrowserSession:
    """Async context manager owning the Playwright stack for one run."""

    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._probe_guard = False
        self._probe_origin: str | None = None
        self._allow_auth_submission = False
        self.blocked_mutations: list[dict[str, str]] = []
        self.blocked_probe_navigations: list[dict[str, str]] = []
        self._public_hosts: set[str] = set()

    async def __aenter__(self) -> BrowserSession:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._config.headless)
        self._context = await self._browser.new_context(
            viewport={
                "width": self._config.viewport.width,
                "height": self._config.viewport.height,
            },
            user_agent=self._config.user_agent,
            accept_downloads=False,
        )
        await self._context.route("**/*", self._guard_request)
        self._context.set_default_navigation_timeout(self._config.navigation_timeout_ms)
        self._context.set_default_timeout(self._config.navigation_timeout_ms)
        return self

    def set_probe_guard(self, enabled: bool, *, source_url: str | None = None) -> None:
        """Block non-idempotent network requests while probing local UI."""
        self._probe_guard = enabled
        self._probe_origin = source_url if enabled else None

    def set_auth_submission_allowed(self, allowed: bool) -> None:
        """Narrow exception used only around an explicitly resumed login."""
        self._allow_auth_submission = allowed

    async def _guard_request(self, route: Route, request: Request) -> None:
        if (
            self._probe_guard
            and self._probe_origin
            and request.is_navigation_request()
            and not is_same_origin(request.url, self._probe_origin)
        ):
            self.blocked_probe_navigations.append(
                {"method": request.method.upper(), "url": request.url}
            )
            await route.abort("blockedbyclient")
            return
        if not self._config.allow_private_networks and request.url.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            host = urlsplit(request.url).hostname or ""
            needs_check = request.is_navigation_request() or host not in self._public_hosts
            if needs_check and not await is_public_destination(request.url):
                await route.abort("blockedbyclient")
                return
            self._public_hosts.add(host)
        method = request.method.upper()
        if (
            self._probe_guard
            and not self._allow_auth_submission
            and method not in {"GET", "HEAD", "OPTIONS"}
        ):
            self.blocked_mutations.append({"method": method, "url": request.url})
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        assert self._context is not None, "BrowserSession used outside its context manager"
        page = await self._context.new_page()
        page.on("dialog", _dismiss_dialog)
        page.on("download", _cancel_download)
        return page


async def _dismiss_dialog(dialog: Dialog) -> None:
    await dialog.dismiss()


async def _cancel_download(download: Download) -> None:
    await download.cancel()
