"""One browser per process, launched at startup, kept for the life of the
process. If it dies, the process exits non-zero and the orchestrator
restarts the container. No relaunch logic, no idle close, no lazy launch.

A warm pool of pre-created contexts (each with a blank page and the init
script installed) sits ready so a render never pays context creation.
Contexts are never reused: a render takes one, uses it, closes it, and the
pool refills in the background.
"""

import asyncio
import logging
import os
import sys
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .config import Settings
from .logs import log_event

CHROMIUM_FIXED_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
)
MUTATION_KEY = "__prerenderLastMutation"

# Installed before any page script runs. Sets the prerender flag, installs a
# first-value-wins setter for the ready flag, and timestamps DOM mutations.
INIT_SCRIPT = """
(() => {
  const READY = %(ready)s, PRERENDER = %(prerender)s, MUTATION = %(mutation)s;
  window[PRERENDER] = true;
  let value;
  Object.defineProperty(window, READY, {
    configurable: false,
    enumerable: true,
    get() { return value; },
    set(v) { if (value === undefined) { value = v; } },
  });
  window[MUTATION] = performance.now();
  new MutationObserver(() => { window[MUTATION] = performance.now(); })
    .observe(document, { childList: true, subtree: true, attributes: true, characterData: true });
})();
"""


def init_script(settings: Settings) -> str:
    import json

    return INIT_SCRIPT % {
        "ready": json.dumps(settings.ready_flag),
        "prerender": json.dumps(settings.prerender_flag),
        "mutation": json.dumps(MUTATION_KEY),
    }


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._pool: asyncio.Queue[tuple[BrowserContext, Page]] = asyncio.Queue()
        self._refill = asyncio.Event()
        self._filler: asyncio.Task[None] | None = None
        self._closing = False
        self._init_script = init_script(settings)

    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def pool_size(self) -> int:
        return self._pool.qsize()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        engine = getattr(self._playwright, self.settings.browser_engine)
        args: list[str] = []
        if self.settings.browser_engine == "chromium":
            args.extend(CHROMIUM_FIXED_ARGS)
        args.extend(self.settings.browser_args)
        self._browser = await engine.launch(headless=True, args=args)
        self._browser.on("disconnected", self._on_disconnected)
        self._filler = asyncio.create_task(self._fill_forever())
        self._refill.set()
        log_event(
            logging.INFO,
            "browser_started",
            engine=self.settings.browser_engine,
            version=self._browser.version,
        )

    async def stop(self) -> None:
        self._closing = True
        if self._filler:
            self._filler.cancel()
        while not self._pool.empty():
            context, _ = self._pool.get_nowait()
            await context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _on_disconnected(self, _browser: Any) -> None:
        if self._closing:
            return
        log_event(
            logging.CRITICAL,
            "browser_died",
            engine=self.settings.browser_engine,
        )
        for handler in logging.getLogger("playwright_prerender").handlers:
            handler.flush()
            handler.close()
        sys.stdout.flush()
        os._exit(1)

    async def _create(self) -> tuple[BrowserContext, Page]:
        assert self._browser is not None
        context = await self._browser.new_context(
            user_agent=self.settings.user_agent,
            extra_http_headers={"X-Prerender-Internal": "1"},
        )
        await context.add_init_script(self._init_script)
        page = await context.new_page()
        return context, page

    async def _fill_forever(self) -> None:
        while True:
            await self._refill.wait()
            self._refill.clear()
            try:
                while self._pool.qsize() < self.settings.concurrency:
                    self._pool.put_nowait(await self._create())
            except Exception:  # noqa: BLE001 - keep the filler alive, whatever failed
                log_event(logging.ERROR, "pool_refill_failed", exc_info=True)
                await asyncio.sleep(0.5)
                self._refill.set()

    async def acquire(self) -> tuple[BrowserContext, Page]:
        """A warm context, or a fresh one if the pool is momentarily empty."""
        try:
            item = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            item = await self._create()
        self._refill.set()
        return item
