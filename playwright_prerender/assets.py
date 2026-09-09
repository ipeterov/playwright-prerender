"""The in-memory asset cache and the Playwright route handler that uses it.

A fresh browser context has an empty HTTP cache, so without help every
render re-downloads the app's bundles. We cache *inputs* (scripts, styles,
fonts) keyed by URL, honouring the origin's Cache-Control, bounded by size
with LRU eviction. Documents are never cached: the point is a fresh page
every time. The same handler aborts requests to BLOCKED_HOSTS.
"""

import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .html import is_html

CACHEABLE_TYPES = frozenset({"script", "stylesheet", "font"})
IMMUTABLE_TTL_S = 365 * 24 * 3600
# Headers that describe the wire encoding of a body we store decoded.
_ENCODING_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)
_MAX_AGE_RE = re.compile(r"\bmax-age\s*=\s*(\d+)", re.IGNORECASE)


def cacheable_ttl(
    status: int, headers: dict[str, str], resource_type: str = "script"
) -> int | None:
    """Seconds this response may be cached, or None if it must not be."""
    if status != 200 or resource_type not in CACHEABLE_TYPES:
        return None
    lower = {k.lower(): v for k, v in headers.items()}
    if "set-cookie" in lower or is_html(lower.get("content-type")):
        return None
    cc = lower.get("cache-control", "").lower()
    if any(token in cc for token in ("no-store", "no-cache", "private")):
        return None
    if "immutable" in cc:
        return IMMUTABLE_TTL_S
    m = _MAX_AGE_RE.search(cc)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    return None


@dataclass
class CachedAsset:
    status: int
    headers: dict[str, str]
    body: bytes
    expires_at: float


@dataclass
class RenderStats:
    hits: int = 0
    misses: int = 0


class AssetCache:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, CachedAsset] = OrderedDict()
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def get(self, url: str) -> CachedAsset | None:
        item = self._items.get(url)
        if item is None:
            return None
        if item.expires_at < time.monotonic():
            self._evict(url)
            return None
        self._items.move_to_end(url)
        return item

    def put(self, url: str, asset: CachedAsset) -> None:
        if len(asset.body) > self.max_bytes:
            return
        if url in self._items:
            self._evict(url)
        self._items[url] = asset
        self.bytes += len(asset.body)
        while self.bytes > self.max_bytes:
            oldest = next(iter(self._items))
            self._evict(oldest)

    def _evict(self, url: str) -> None:
        item = self._items.pop(url)
        self.bytes -= len(item.body)


@dataclass
class RouteHandler:
    """Bound to one render: aborts blocked hosts, serves and fills the cache."""

    cache: AssetCache | None
    blocked_hosts: frozenset[str]
    stats: RenderStats = field(default_factory=RenderStats)

    async def __call__(self, route: Any) -> None:
        try:
            await self._handle(route)
        except PlaywrightError:
            # The render finished and closed its context while this request
            # was still in flight. Nothing is left to serve it to.
            return

    async def _handle(self, route: Any) -> None:
        request = route.request
        if urlsplit(request.url).hostname in self.blocked_hosts:
            await route.abort()
            return
        if (
            self.cache is None
            or request.method != "GET"
            or request.resource_type not in CACHEABLE_TYPES
        ):
            await route.continue_()
            return

        hit = self.cache.get(request.url)
        if hit is not None:
            self.stats.hits += 1
            await route.fulfill(
                status=hit.status, headers=hit.headers, body=hit.body
            )
            return

        self.stats.misses += 1
        response = await route.fetch()
        body = await response.body()
        ttl = cacheable_ttl(
            response.status, response.headers, request.resource_type
        )
        if ttl is not None:
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in _ENCODING_HEADERS
            }
            self.cache.put(
                request.url,
                CachedAsset(
                    status=response.status,
                    headers=headers,
                    body=body,
                    expires_at=time.monotonic() + ttl,
                ),
            )
        await route.fulfill(response=response, body=body)
