# Manages one Playwright Chromium browser + page.
# Runs with --remote-debugging-port so a human operator can attach
# DevTools to the live session during an escalation handoff.

from __future__ import annotations

import os
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


class BrowserSession:
    def __init__(
        self,
        headless: bool = False,
        cdp_port: int = 9222,
        trace_dir: Path | None = None,
    ):
        self._headless = headless
        self._cdp_port = cdp_port
        self._trace_dir = trace_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[f"--remote-debugging-port={self._cdp_port}"],
        )
        self._context = await self._browser.new_context()
        if self._trace_dir:
            self._trace_dir.mkdir(parents=True, exist_ok=True)
            # screenshots=True, snapshots=True captures DOM + visual state for debugging
            await self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
        self._page = await self._context.new_page()

    async def stop(self, trace_path: Path | None = None) -> None:
        if self._context and self._trace_dir and trace_path:
            await self._context.tracing.stop(path=str(trace_path))
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Session not started — call await session.start() first")
        return self._page

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self._cdp_port}"

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path))

    @classmethod
    def from_env(cls, trace_dir: Path | None = None) -> "BrowserSession":
        return cls(
            headless=os.environ.get("BROWSER_HEADLESS", "false").lower() == "true",
            cdp_port=int(os.environ.get("BROWSER_CDP_PORT", "9222")),
            trace_dir=trace_dir,
        )
