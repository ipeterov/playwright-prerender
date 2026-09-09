"""The render coroutine: navigate, wait for the page to say it's ready,
wait for the DOM to go quiet, then read the status and the final URL."""

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import MUTATION_KEY
from .config import Settings
from .html import MetaStatus, meta_status, path_and_query
from .timing import Timeline

ALLOWED_STATUSES = frozenset({200, 404, 410, 301})


@dataclass(frozen=True)
class RenderResult:
    status: int
    html: str
    location: str | None
    final_path: str


class RenderTimeout(Exception):
    """The wait condition didn't happen in time. Carries what rendered so far."""

    def __init__(self, reason: str, partial_html: str) -> None:
        super().__init__(reason)
        self.partial_html = partial_html


def resolve_status(
    flag_value: Any, meta: MetaStatus, requested_path: str, final_path: str
) -> tuple[int, str | None]:
    """An explicit status from the flag wins; a bare `true` (or no flag at
    all) defers to the Prerender.io meta tags; otherwise 200. Then the
    router-redirect rule."""
    status: int | None = None
    location: str | None = None
    if (
        isinstance(flag_value, dict)
        and flag_value.get("status") in ALLOWED_STATUSES
    ):
        status = int(flag_value["status"])
        loc = flag_value.get("location")
        location = loc if isinstance(loc, str) and loc else None
    if status is None and meta.status in ALLOWED_STATUSES:
        status = meta.status
        location = meta.location
    if status is None:
        status = 200

    if status == 301 and not location:
        status = 200
    if status == 200 and final_path != requested_path:
        return 301, final_path
    if status == 301:
        return 301, location
    return status, None


class _Deadline:
    def __init__(self, total_ms: int) -> None:
        self._end = asyncio.get_running_loop().time() + total_ms / 1000

    def remaining_ms(self) -> float:
        return max(1.0, (self._end - asyncio.get_running_loop().time()) * 1000)


async def render(
    page: Page,
    settings: Settings,
    url: str,
    requested_path: str,
    viewport: tuple[int, int],
    *,
    timeline: Timeline,
) -> RenderResult:
    deadline = _Deadline(settings.timeout_ms)
    # Per render on the pooled page, so one pool serves both viewports.
    await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    ready_expr = (
        f"() => window[{json.dumps(settings.ready_flag)}] !== undefined"
    )

    try:
        if settings.wait_for == "flag" or settings.wait_selector is not None:
            # Don't wait for images before we start watching for the flag.
            await page.goto(
                url, wait_until="commit", timeout=deadline.remaining_ms()
            )
            if settings.wait_for == "flag":
                await page.wait_for_function(
                    ready_expr, timeout=deadline.remaining_ms()
                )
            else:
                await page.wait_for_selector(
                    settings.wait_selector,
                    state="attached",
                    timeout=deadline.remaining_ms(),
                )
        else:
            await page.goto(
                url,
                wait_until=settings.wait_for,
                timeout=deadline.remaining_ms(),
            )  # type: ignore[arg-type]
    except PlaywrightTimeoutError as e:
        partial = await _content_or_empty(page)
        raise RenderTimeout(
            f"{settings.wait_for} not satisfied within {settings.timeout_ms}ms",
            partial,
        ) from e
    timeline.mark("flag")

    await settle(page, settings, deadline)
    timeline.mark("settled")

    flag_value = await page.evaluate(
        f"() => window[{json.dumps(settings.ready_flag)}]"
    )
    html = await page.content()
    timeline.mark("extracted")
    final_path = path_and_query(page.url)
    status, location = resolve_status(
        flag_value, meta_status(html), requested_path, final_path
    )
    return RenderResult(
        status=status, html=html, location=location, final_path=final_path
    )


async def settle(page: Page, settings: Settings, deadline: _Deadline) -> None:
    """Wait for no DOM mutations for SETTLE_QUIET_MS, capped at SETTLE_MAX_MS."""
    quiet_expr = (
        f"(q) => performance.now() - window[{json.dumps(MUTATION_KEY)}] >= q"
    )
    budget = min(settings.settle_max_ms, deadline.remaining_ms())
    with contextlib.suppress(PlaywrightTimeoutError):
        await page.wait_for_function(
            quiet_expr, arg=settings.settle_quiet_ms, timeout=budget, polling=50
        )


async def _content_or_empty(page: Page) -> str:
    try:
        return await page.content()
    except Exception:  # noqa: BLE001 - a page mid-navigation may refuse; nothing to snapshot
        return ""
