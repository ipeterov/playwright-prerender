"""FastAPI routes and the request flow.

One fetch of the origin per request. The browser's own document request is
intercepted and performed by us: the response decides whether there is
anything to render (a 200 HTML page) or whether it goes straight back to the
crawler unchanged (robots.txt, sitemaps, redirects, real 404s). Either way
the origin renders the page once.
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import sentry_sdk
from fastapi import FastAPI, Request, Response
from playwright.async_api import Error as PlaywrightError

from .assets import AssetCache, RenderStats, RouteHandler
from .browser import BrowserManager
from .config import INTERNAL_HEADER, Settings
from .html import filter_headers, is_html, strip_query_params, strip_scripts
from .logs import log_event
from .render import RenderResult, RenderTimeout, render
from .timing import OriginRequests, Timeline

# `none` is shorthand for `noindex, nofollow`.
_NOINDEX_RE = re.compile(r"\b(noindex|none)\b", re.IGNORECASE)


@dataclass
class OriginResponse:
    """What the origin said for the requested URL, however we fetched it."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def content_type(self) -> str | None:
        return next(
            (v for k, v in self.headers if k.lower() == "content-type"), None
        )

    @property
    def noindex(self) -> bool:
        """The origin told crawlers not to index this. Rendering it would
        only cost a browser session for a page no crawler will keep."""
        tag = next(
            (v for k, v in self.headers if k.lower() == "x-robots-tag"), ""
        )
        return bool(_NOINDEX_RE.search(tag))

    @property
    def renderable(self) -> bool:
        return (
            self.status == 200
            and is_html(self.content_type)
            and not self.noindex
        )


@dataclass
class DocumentCapture:
    """Filled in by the document route handler during a render."""

    response: OriginResponse | None = None
    error: str | None = None
    # The document request we answered; later navigations (a client-side
    # redirect that reloads) fetch normally and are not captured.
    seen: int = field(default=0)


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
        self.origin_host = urlsplit(settings.origin).hostname or ""
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

    async def fetch_origin(self, method: str, url: str) -> OriginResponse:
        """A plain HTTP fetch, for the requests that never involve a browser:
        HEAD, and paths the rules exclude from rendering."""
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
        return OriginResponse(
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


def passthrough(state: State, origin: OriginResponse) -> Response:
    headers = dict(filter_headers(origin.headers, state.settings.drop_headers))
    return Response(
        content=origin.body, status_code=origin.status, headers=headers
    )


def origin_unreachable(detail: str) -> Response:
    return Response(
        content=f"Origin unreachable: {detail}\n".encode(),
        status_code=502,
        headers={"Content-Type": "text/plain"},
    )


def rendered_response(
    state: State, origin: OriginResponse, result: RenderResult
) -> Response:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    robots = next(
        (v for k, v in origin.headers if k.lower() == "x-robots-tag"), None
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


def fallback(
    state: State, origin: OriginResponse | None, partial_html: str | None
) -> Response:
    """What to serve when the render didn't happen, per ON_TIMEOUT."""
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
    if origin is None:
        # The document never arrived, so there is no shell to fall back to.
        return origin_unreachable("no response before the timeout")
    return passthrough(state, origin)


async def handle_request(state: State, request: Request) -> Response:
    settings = state.settings
    timeline = Timeline()
    path = request.url.path
    received = path + (f"?{request.url.query}" if request.url.query else "")
    # What the origin sees, and what the browser navigates to: the URL as
    # received minus any parameter a proxy rule uses to route here.
    requested = strip_query_params(received, settings.strip_query_params)
    url = settings.origin + requested
    fields: dict[str, Any] = {
        "path": received,
        "engine": settings.browser_engine,
    }

    def finish(
        response: Response,
        outcome: str,
        level: int = logging.INFO,
        **extra: Any,
    ) -> Response:
        timeline.mark("done")
        log_event(
            level,
            "request",
            **fields,
            outcome=outcome,
            status=response.status_code,
            **timeline.fields(),
            **extra,
        )
        return response

    if INTERNAL_HEADER.lower() in request.headers:
        # One of our own fetches came back to us: the proxy in front routes
        # on something this service forwards. Answer without touching the
        # origin, so a misconfigured rule costs one fast, visible error
        # rather than a chain of renders each waiting on the next.
        state.last_error = "loop"
        return finish(
            Response(
                content=b"Loop detected: the proxy routed the renderer's own "
                b"fetch back to it.\n",
                status_code=508,
                headers={"Content-Type": "text/plain"},
            ),
            "loop",
            logging.ERROR,
        )

    if request.method == "HEAD" or not state.path_permitted(path):
        # No browser involved: a plain fetch, passed through.
        try:
            origin = await state.fetch_origin(request.method, url)
        except httpx.HTTPError as e:
            state.last_error = f"origin: {e!r}"
            return finish(
                origin_unreachable(repr(e)),
                "error",
                logging.ERROR,
                error=f"origin: {e!r}",
            )
        return finish(passthrough(state, origin), "passthrough")

    fields["wait_for"] = settings.wait_for
    kind, viewport = settings.viewport_for(
        request.headers.get("user-agent", "")
    )
    fields["viewport"] = f"{kind}:{viewport[0]}x{viewport[1]}"
    try:
        await asyncio.wait_for(
            state.slots.acquire(), timeout=settings.queue_wait_ms / 1000
        )
    except TimeoutError:
        state.last_error = "queue_full"
        return finish(
            fallback(state, None, None), "queue_full", logging.WARNING
        )
    timeline.mark("slot")

    state.in_flight += 1
    stats = RenderStats()
    capture = DocumentCapture()
    api = OriginRequests(state.origin_host)

    async def document(route: Any) -> None:
        """The browser's document request: fetch it ourselves, once, with
        redirects left unfollowed so the crawler sees them. A response that
        isn't a 200 HTML page is kept for passthrough and the navigation is
        aborted; a page is handed to the browser to render."""
        capture.seen += 1
        if capture.seen > 1:
            # A reload the page itself asked for. Not ours to judge.
            await route.continue_()
            return
        try:
            response = await route.fetch(max_redirects=0)
            body = await response.body()
        except PlaywrightError as e:
            capture.error = repr(e)
            await route.abort()
            return
        capture.response = OriginResponse(
            status=response.status,
            headers=list(response.headers.items()),
            body=body,
        )
        timeline.mark("origin")
        if not capture.response.renderable:
            await route.abort()
            return
        await route.fulfill(response=response, body=body)

    def on_request_finished(finished: Any) -> None:
        timing = finished.timing
        duration = timing["responseEnd"] - timing["requestStart"]
        if duration >= 0:
            api.record(
                urlsplit(finished.url).hostname or "",
                finished.resource_type,
                duration,
            )

    def render_fields() -> dict[str, Any]:
        return {
            "asset_cache_hits": stats.hits,
            "asset_cache_misses": stats.misses,
            **api.fields(),
        }

    try:
        context, page = await state.browser.acquire()
        timeline.mark("context")
        try:
            await context.route(
                "**/*",
                RouteHandler(
                    state.asset_cache,
                    state.blocked_hosts,
                    document=document,
                    stats=stats,
                ),
            )
            page.on("requestfinished", on_request_finished)
            result = await render(
                page, settings, url, requested, viewport, timeline=timeline
            )
        finally:
            await context.close()
    except RenderTimeout as e:
        state.last_error = str(e)
        return finish(
            fallback(state, capture.response, e.partial_html),
            "timeout",
            logging.WARNING,
            error=str(e),
            **render_fields(),
        )
    except Exception as e:  # noqa: BLE001 - every failure has a fallback
        if capture.response is not None and not capture.response.renderable:
            # We aborted the navigation on purpose: the origin's answer goes
            # back to the crawler as it came.
            return finish(
                passthrough(state, capture.response),
                "passthrough",
                **({"reason": "noindex"} if capture.response.noindex else {}),
                **render_fields(),
            )
        if capture.error is not None:
            state.last_error = f"origin: {capture.error}"
            return finish(
                origin_unreachable(capture.error),
                "error",
                logging.ERROR,
                error=f"origin: {capture.error}",
                **render_fields(),
            )
        state.last_error = repr(e)
        sentry_sdk.capture_exception(e)
        return finish(
            fallback(state, capture.response, None),
            "error",
            logging.ERROR,
            error=repr(e),
            **render_fields(),
        )
    finally:
        state.in_flight -= 1
        state.slots.release()

    state.renders_total += 1
    assert capture.response is not None  # a render implies a document
    return finish(
        rendered_response(state, capture.response, result),
        "rendered",
        html_bytes=len(result.html),
        **render_fields(),
    )
