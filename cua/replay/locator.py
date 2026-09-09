# Resolves a LocatorSpec to a Playwright Locator, trying strategies in order.
# Non-text strategies (CSS, XPath, ARIA) that match multiple elements raise
# AmbiguousLocatorError — a deterministic replay can't safely pick one.
# Waits for visibility before counting so slow-loading elements aren't missed.

from __future__ import annotations

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from cua.artifact.schema import LocatorMethod, LocatorSpec, LocatorStrategy


class ElementNotFoundError(Exception):
    pass


class AmbiguousLocatorError(Exception):
    pass


_TEXT_METHODS = {LocatorMethod.TEXT}
_UNIQUE_METHODS = {
    LocatorMethod.ARIA_ROLE, LocatorMethod.ARIA_LABEL, LocatorMethod.PLACEHOLDER,
    LocatorMethod.XPATH, LocatorMethod.CSS, LocatorMethod.TITLE,
}
_VISIBILITY_TIMEOUT_MS = 5_000


async def resolve_locator(page: Page, spec: LocatorSpec) -> Locator:
    strategies = [spec.primary] + spec.fallbacks
    last_exc: Exception = ElementNotFoundError("No strategies defined")

    for strategy in strategies:
        try:
            loc = _build_locator(page, strategy)
            try:
                await loc.first.wait_for(state="visible", timeout=_VISIBILITY_TIMEOUT_MS)
            except PlaywrightTimeout:
                last_exc = ElementNotFoundError(
                    f"No element became visible for "
                    f"{strategy.method.value}:{strategy.value!r} within {_VISIBILITY_TIMEOUT_MS}ms"
                )
                continue

            count = await loc.count()
            if count > 1 and strategy.method in _UNIQUE_METHODS:
                raise AmbiguousLocatorError(
                    f"{strategy.method.value}:{strategy.value!r} matched {count} elements — "
                    f"use a more specific locator (e.g. XPath scoped to a table row)."
                )
            return loc.first

        except (AmbiguousLocatorError, ElementNotFoundError):
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise ElementNotFoundError(
        f"All {len(strategies)} strategy(ies) failed. "
        f"Primary: {spec.primary.method.value}:{spec.primary.value!r}. "
        f"Last error: {last_exc}"
    ) from last_exc


def _build_locator(page: Page, strategy: LocatorStrategy) -> Locator:
    m = strategy.method
    if m == LocatorMethod.ARIA_ROLE:
        return page.get_by_role(strategy.role or strategy.value, name=strategy.value, exact=strategy.exact)  # type: ignore[arg-type]
    if m == LocatorMethod.ARIA_LABEL:
        return page.get_by_label(strategy.value, exact=strategy.exact)
    if m == LocatorMethod.TEXT:
        return page.get_by_text(strategy.value, exact=strategy.exact)
    if m == LocatorMethod.PLACEHOLDER:
        return page.get_by_placeholder(strategy.value, exact=strategy.exact)
    if m == LocatorMethod.TITLE:
        return page.get_by_title(strategy.value, exact=strategy.exact)
    if m == LocatorMethod.XPATH:
        return page.locator(f"xpath={strategy.value}")
    if m == LocatorMethod.CSS:
        return page.locator(strategy.value)
    raise ValueError(f"Unknown locator method: {m}")
