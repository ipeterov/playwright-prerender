"""FastAPI routes and the request flow: probe the origin, pass through
anything that isn't a 200 HTML document, otherwise render it."""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import sentry_sdk
from fastapi import FastAPI, Request, Response

from .assets import AssetCache, RenderStats, RouteHandler
from .browser import BrowserManager
from .config import INTERNAL_HEADER, Settings
from .html import filter_headers, is_html, strip_scripts
from .logs import log_event
from .render import RenderResult, RenderTimeout, render


@dataclass
class Probe:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def content_type(self) -> str | None:
        return next(
            (v for k, v in self.headers if k.lower() == "content-type"), None
        )

    @property
    def renderable(self) -> bool:
        return self.status == 200 and is_html(self.content_type)


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.browser = BrowserManager(settings)
        self.asset_cache = (
            AssetCache(settings.asset_cache_mb * 1024 * 1024)
            if settings.asset_cache_mb > 0
            else None
        )
        self.blocked_hosts = frozenset(settings.blocked_hosts)
        self.slots = asyncio.Semaphore(settings.concurrency)
        self.transport = httpx.AsyncHTTPTransport(retries=0)
        self.started_at = time.monotonic()
        self.in_flight = 0
        self.renders_total = 0
        self.last_error: str | None = None
        self.path_allow = (
            re.compile(settings.path_allow) if settings.path_allow else None
        )
        self.path_deny = (
            re.compile(settings.path_deny) if settings.path_deny else None
        )

    def path_permitted(self, path: str) -> bool:
        if self.path_deny and self.path_deny.search(path):
            return False
        if self.path_allow and not self.path_allow.search(path):
            return False
        return True

    async def probe(self, method: str, url: str) -> Probe:
        # A new client per request means a fresh cookie jar; the shared
        # transport keeps the connection pool.
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=httpx.Timeout(self.settings.timeout_ms / 1000),
            headers={
                "User-Agent": self.settings.user_agent,
                INTERNAL_HEADER: "1",
            },
        ) as client:
            try:
                response = await client.request(method, url)
            except httpx.TransportError:
                # Typically a pooled keep-alive connection the origin closed
                # under us. GET/HEAD are idempotent: one retry on a fresh
                # connection, then give up.
                response = await client.request(method, url)
        return Probe(
            status=response.status_code,
            headers=list(response.headers.items()),
            body=response.content,
        )


def create_app(settings: Settings) -> FastAPI:
    state = State(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.sentry_dsn:
            sentry_sdk.init(
                dsn=settings.sentry_dsn, release=settings.release or None
            )
        log_event(logging.INFO, "startup", **settings.masked())
        await state.browser.start()
        try:
            yield
        finally:
            await state.browser.stop()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.prerender = state

    @app.get("/health/")
    async def health() -> dict[str, Any]:
        return {
            "browser": "running" if state.browser.running else "stopped",
            "in_flight": state.in_flight,
            "uptime_s": int(time.monotonic() - state.started_at),
            "pool_size": state.browser.pool_size,
            "asset_cache_bytes": state.asset_cache.bytes
            if state.asset_cache
            else 0,
            "renders_total": state.renders_total,
            "last_error": state.last_error,
        }

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def handle(request: Request, path: str) -> Response:
        return await handle_request(state, request)

    return app


def passthrough(
    state: State, probe: Probe, status: int | None = None
) -> Response:
    headers = dict(filter_headers(probe.headers, state.settings.drop_headers))
    return Response(
        content=probe.body, status_code=status or probe.status, headers=headers
    )


def rendered_response(
    state: State, probe: Probe, result: RenderResult
) -> Response:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    robots = next(
        (v for k, v in probe.headers if k.lower() == "x-robots-tag"), None
    )
    if robots:
        headers["X-Robots-Tag"] = robots
    if result.status == 301:
        headers["Location"] = result.location or "/"
        return Response(content=b"", status_code=301, headers=headers)
    return Response(
        content=strip_scripts(result.html),
        status_code=result.status,
        headers=headers,
    )


def fallback(state: State, probe: Probe, partial_html: str | None) -> Response:
    mode = state.settings.on_timeout
    if mode == "503":
        return Response(
            content=b"Render failed; retry later.\n",
            status_code=503,
            headers={"Retry-After": "30", "Content-Type": "text/plain"},
        )
    if mode == "snapshot" and partial_html:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        return Response(
            content=strip_scripts(partial_html),
            status_code=200,
            headers=headers,
        )
    return passthrough(state, probe)


async def handle_request(state: State, request: Request) -> Response:
    settings = state.settings
    started = time.monotonic()
    path = request.url.path
    requested = path + (f"?{request.url.query}" if request.url.query else "")
    url = settings.origin + requested
    fields: dict[str, Any] = {
        "path": requested,
        "engine": settings.browser_engine,
    }

    def finish(
        response: Response,
        outcome: str,
        level: int = logging.INFO,
        **extra: Any,
    ) -> Response:
        ms = int((time.monotonic() - started) * 1000)
        log_event(
            level,
            "request",
            **fields,
            outcome=outcome,
            status=response.status_code,
            ms=ms,
            **extra,
        )
        return response

    try:
        probe = await state.probe(request.method, url)
    except httpx.HTTPError as e:
        # No origin response at all, so nothing to pass through.
        state.last_error = f"probe: {e!r}"
        return finish(
            Response(
                content=b"Origin unreachable.\n",
                status_code=502,
                headers={"Content-Type": "text/plain"},
            ),
            "error",
            logging.ERROR,
            error=f"probe: {e!r}",
        )
    if (
        request.method == "HEAD"
        or INTERNAL_HEADER.lower() in request.headers
        or not probe.renderable
        or not state.path_permitted(path)
    ):
        return finish(passthrough(state, probe), "passthrough")

    fields["wait_for"] = settings.wait_for
    try:
        await asyncio.wait_for(
            state.slots.acquire(), timeout=settings.queue_wait_ms / 1000
        )
    except TimeoutError:
        state.last_error = "queue_full"
        return finish(
            fallback(state, probe, None), "queue_full", logging.WARNING
        )

    state.in_flight += 1
    stats = RenderStats()
    try:
        context, page = await state.browser.acquire()
        try:
            if state.asset_cache is not None or state.blocked_hosts:
                await context.route(
                    "**/*",
                    RouteHandler(state.asset_cache, state.blocked_hosts, stats),
                )
            result = await render(page, settings, url, requested)
        finally:
            await context.close()
    except RenderTimeout as e:
        state.last_error = str(e)
        return finish(
            fallback(state, probe, e.partial_html),
            "timeout",
            logging.WARNING,
            error=str(e),
            asset_cache_hits=stats.hits,
            asset_cache_misses=stats.misses,
        )
    except Exception as e:  # noqa: BLE001 - any renderer failure falls back
        state.last_error = repr(e)
        sentry_sdk.capture_exception(e)
        return finish(
            fallback(state, probe, None),
            "error",
            logging.ERROR,
            error=repr(e),
            asset_cache_hits=stats.hits,
            asset_cache_misses=stats.misses,
        )
    finally:
        state.in_flight -= 1
        state.slots.release()

    state.renders_total += 1
    return finish(
        rendered_response(state, probe, result),
        "rendered",
        asset_cache_hits=stats.hits,
        asset_cache_misses=stats.misses,
    )
