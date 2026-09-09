# Snapshots the current page state for the agent — ARIA tree + screenshot.
# Uses page.aria_snapshot() (Playwright ≥ 1.47) which gives structured
# role/name text rather than raw HTML, so the model can target elements
# by meaning rather than by position.

from __future__ import annotations

import base64

from playwright.async_api import Page


async def observe(page: Page) -> tuple[str, str]:
    """Returns (observation_text, screenshot_base64)."""
    url = page.url
    try:
        title = await page.title()
    except Exception:
        title = "(unknown)"

    try:
        tree = await page.aria_snapshot()
    except Exception:
        try:
            tree = await _dom_text_fallback(page)
        except Exception:
            tree = "(accessibility tree unavailable)"

    observation = f"URL: {url}\nTitle: {title}\n\nAccessibility Tree:\n{tree}"
    screenshot_b64 = base64.b64encode(await page.screenshot(full_page=False)).decode()
    return observation, screenshot_b64


async def _dom_text_fallback(page: Page) -> str:
    # Walk the DOM and pull out interactive elements when aria_snapshot isn't available.
    return await page.evaluate("""() => {
        const lines = [];
        const walk = (node, depth) => {
            if (!node || depth > 8) return;
            const tag = (node.tagName || '').toLowerCase();
            const interactable = ['input','button','select','textarea','a','label',
                                   'h1','h2','h3','h4','th','td'];
            if (interactable.includes(tag)) {
                const name = node.getAttribute('name') || node.getAttribute('id') ||
                             node.getAttribute('placeholder') || node.getAttribute('aria-label') ||
                             (node.textContent || '').trim().slice(0, 80);
                if (name) lines.push('  '.repeat(depth) + tag + ': "' + name.trim() + '"');
            }
            for (const child of node.children || []) walk(child, depth + 1);
        };
        walk(document.body, 0);
        return lines.slice(0, 150).join('\\n') || '(no interactive elements found)';
    }""")
