import time

from playwright_prerender.assets import AssetCache, CachedAsset, cacheable_ttl


def asset(size: int, ttl: float = 3600) -> CachedAsset:
    return CachedAsset(
        status=200,
        headers={"content-type": "application/javascript"},
        body=b"x" * size,
        expires_at=time.monotonic() + ttl,
    )


def test_cacheable_ttl_rules():
    js = {"content-type": "application/javascript"}
    assert cacheable_ttl(200, {**js, "cache-control": "max-age=600"}) == 600
    assert (
        cacheable_ttl(200, {**js, "cache-control": "public, immutable"})
        == 365 * 24 * 3600
    )
    assert cacheable_ttl(200, {**js, "Cache-Control": "Max-Age=5"}) == 5
    # Never: no-store / private / no-cache / max-age=0 / no header at all.
    assert cacheable_ttl(200, {**js, "cache-control": "no-store"}) is None
    assert (
        cacheable_ttl(200, {**js, "cache-control": "private, max-age=9"})
        is None
    )
    assert cacheable_ttl(200, {**js, "cache-control": "no-cache"}) is None
    assert cacheable_ttl(200, {**js, "cache-control": "max-age=0"}) is None
    assert cacheable_ttl(200, js) is None
    # Never: non-200, Set-Cookie, documents, non-static resource types.
    assert cacheable_ttl(304, {**js, "cache-control": "max-age=9"}) is None
    assert (
        cacheable_ttl(
            200, {**js, "cache-control": "max-age=9", "set-cookie": "a=b"}
        )
        is None
    )
    assert (
        cacheable_ttl(
            200, {"content-type": "text/html", "cache-control": "max-age=9"}
        )
        is None
    )
    assert (
        cacheable_ttl(200, {**js, "cache-control": "max-age=9"}, "document")
        is None
    )
    assert (
        cacheable_ttl(200, {**js, "cache-control": "max-age=9"}, "image")
        is None
    )


def test_lru_bound_and_eviction_order():
    cache = AssetCache(max_bytes=100)
    cache.put("a", asset(40))
    cache.put("b", asset(40))
    assert cache.bytes == 80
    cache.get("a")  # a is now most recently used
    cache.put("c", asset(40))  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.bytes == 80
    assert len(cache) == 2


def test_oversized_item_is_skipped_and_replacement_accounts_bytes():
    cache = AssetCache(max_bytes=50)
    cache.put("big", asset(51))
    assert len(cache) == 0
    cache.put("a", asset(10))
    cache.put("a", asset(20))
    assert cache.bytes == 20


def test_expired_item_is_dropped_on_read():
    cache = AssetCache(max_bytes=100)
    cache.put("a", asset(10, ttl=-1))
    assert cache.get("a") is None
    assert cache.bytes == 0
