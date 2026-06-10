"""Playwright browser lifecycle with engine-wide guards.

Guards applied to every page:
- native dialogs (alert/confirm/prompt) are auto-dismissed so they never
  block the agent
- downloads are cancelled (the agent maps states; it never saves files)
"""

from __future__ import annotations

from types import TracebackType

from playwright.async_api import Browser, BrowserContext, Dialog, Download, Page, async_playwright

from engine.schemas import BrowserConfig


class BrowserSession:
    """Async context manager owning the Playwright stack for one run."""

    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._config.headless)
        self._context = await self._browser.new_context(
            viewport={
                "width": self._config.viewport.width,
                "height": self._config.viewport.height,
            },
            user_agent=self._config.user_agent,
        )
        self._context.set_default_navigation_timeout(self._config.navigation_timeout_ms)
        self._context.set_default_timeout(self._config.navigation_timeout_ms)
        return self

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
