# Manages one Playwright Chromium browser + page.
# Runs with --remote-debugging-port so a human operator can attach DevTools
# during an escalation handoff on the same live session.
# When capture_snapshots=False (recommended when sensitive params are present),
# DOM snapshots are excluded from the trace to avoid persisting sensitive page content.

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
        capture_snapshots: bool = True,
    ):
        self._headless = headless
        self._cdp_port = cdp_port
        self._trace_dir = trace_dir
        self._capture_snapshots = capture_snapshots
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
            await self._context.tracing.start(
                screenshots=True,
                # Disable DOM snapshots when sensitive data is present; screenshots are still kept
                # so the trace is still useful for debugging without exposing form field values.
                snapshots=self._capture_snapshots,
                sources=False,
            )
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

    async def install_domain_guard(self, is_forbidden_fn) -> None:
        """
        Install a Playwright route handler that aborts document navigations to
        forbidden domains before they happen.  is_forbidden_fn(url) -> str | None.
        This provides a continuous navigation boundary rather than post-facto checks.
        """
        async def handler(route, request):
            if request.resource_type == "document":
                err = is_forbidden_fn(request.url)
                if err:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()
        await self.page.route("**/*", handler)

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path))

    @classmethod
    def from_env(cls, trace_dir: Path | None = None, capture_snapshots: bool = True) -> "BrowserSession":
        return cls(
            headless=os.environ.get("BROWSER_HEADLESS", "false").lower() == "true",
            cdp_port=int(os.environ.get("BROWSER_CDP_PORT", "9222")),
            trace_dir=trace_dir,
            capture_snapshots=capture_snapshots,
        )
