"""End to end against the stub app: every behaviour in the HTTP contract."""

import asyncio

import httpx
import pytest

from .stub_app.app import hits

# --- health ---------------------------------------------------------------


async def test_health(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/health/")
    assert r.status_code == 200
    body = r.json()
    assert body["browser"] == "running"
    assert body["in_flight"] == 0
    assert set(body) >= {
        "uptime_s",
        "pool_size",
        "asset_cache_bytes",
        "renders_total",
        "last_error",
    }


# --- rendering ------------------------------------------------------------


async def test_renders_flag_page_and_strips_scripts(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert "Hello from home" in r.text
    assert "Loading…" not in r.text
    assert "<script src" not in r.text
    assert "setReady" not in r.text
    assert "application/ld+json" in r.text
    assert 'rel="stylesheet"' in r.text


async def test_dom_quiet_catches_content_committed_after_flag(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/late/")
    assert r.status_code == 200
    assert "Late content" in r.text
    assert 'id="nested"' in r.text


async def test_first_value_wins_404_beats_later_redirect(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/dead/")
    assert r.status_code == 404
    assert "Listing" in r.text or "Not found" in r.text


async def test_410_from_flag(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/gone/")
    assert r.status_code == 410
    assert "Gone" in r.text


async def test_301_from_flag(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/moved/")
    assert r.status_code == 301
    assert r.headers["location"] == "/target/"


async def test_router_redirect_becomes_301(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/router/")
    assert r.status_code == 301
    assert r.headers["location"] == "/router/intro/"


async def test_prerender_io_meta_tags(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/meta404/")
    assert r.status_code == 404
    assert "meta says gone" in r.text
    r = await client.get(f"{service}/meta301/")
    assert r.status_code == 301
    assert r.headers["location"] == "/elsewhere/"


async def test_prerender_flag_lets_app_skip_widgets(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/widgets/")
    assert r.status_code == 200
    assert "chat-widget" not in r.text
    assert "<p>content</p>" in r.text


async def test_query_string_reaches_the_page(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/query/?q=hello")
    assert r.status_code == 200
    assert "q=hello" in r.text


async def test_blocked_host_is_aborted(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/blocked/")
    assert r.status_code == 200
    assert "blocked script errored" in r.text


async def test_x_robots_tag_copied_from_probe(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/private/")
    assert r.status_code == 200
    assert r.headers["x-robots-tag"] == "noindex"
    assert "set-cookie" not in r.headers
    assert "server-timing" not in r.headers


async def test_asset_cache_serves_second_render_from_memory(
    service: str, client: httpx.AsyncClient
):
    before = hits.get("/assets/app.js", 0)
    css_before = hits.get("/assets/app.css", 0)
    r1 = await client.get(f"{service}/assets-count/")
    r2 = await client.get(f"{service}/assets-count/")
    assert "js loaded 1" in r1.text
    assert "js loaded 1" in r2.text
    # The immutable bundle is fetched at most once across both renders
    # (the first-ever render in the session may have warmed it already).
    assert hits["/assets/app.js"] - before <= 1
    # no-store CSS is fetched every time.
    assert hits["/assets/app.css"] - css_before == 2
    health = (await client.get(f"{service}/health/")).json()
    assert health["asset_cache_bytes"] > 0


GOOGLEBOT_SMARTPHONE = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 "
    "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
GOOGLEBOT_DESKTOP = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html) Chrome/120.0.0.0 Safari/537.36"
)


async def test_viewport_follows_the_crawler_user_agent(
    service: str, client: httpx.AsyncClient
):
    desktop = await client.get(
        f"{service}/responsive/", headers={"User-Agent": GOOGLEBOT_DESKTOP}
    )
    assert 'id="navbar"' in desktop.text
    assert "<p>1024x4096</p>" in desktop.text

    mobile = await client.get(
        f"{service}/responsive/", headers={"User-Agent": GOOGLEBOT_SMARTPHONE}
    )
    assert 'id="hamburger"' in mobile.text
    assert "<p>412x4096</p>" in mobile.text

    # No user agent at all is a desktop client.
    anon = await client.get(
        f"{service}/responsive/", headers={"User-Agent": ""}
    )
    assert 'id="navbar"' in anon.text


async def test_viewport_settings_override(
    service_factory, client: httpx.AsyncClient
):
    svc = await service_factory(viewport="800x600", mobile_ua_regex="")
    r = await client.get(
        f"{svc}/responsive/", headers={"User-Agent": GOOGLEBOT_SMARTPHONE}
    )
    assert "<p>800x600</p>" in r.text
    assert 'id="navbar"' in r.text


# --- passthrough ----------------------------------------------------------


async def test_non_html_passes_through(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Disallow: /private/" in r.text


async def test_origin_redirect_passes_through(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/old/")
    assert r.status_code == 301
    assert r.headers["location"] == "/new/"


async def test_real_404_passes_through(service: str, client: httpx.AsyncClient):
    r = await client.get(f"{service}/does-not-exist/")
    assert r.status_code == 404
    assert "Real 404" in r.text


async def test_head_is_never_rendered(service: str, client: httpx.AsyncClient):
    r = await client.head(f"{service}/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.content == b""


async def test_internal_header_is_a_loop_guard(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(f"{service}/", headers={"X-Prerender-Internal": "1"})
    assert r.status_code == 200
    assert "Loading…" in r.text  # the raw shell, not a render


async def test_probe_uses_own_ua_header_and_no_crawler_cookies(
    service: str, client: httpx.AsyncClient
):
    r = await client.get(
        f"{service}/echo-headers/",
        headers={"User-Agent": "Googlebot/2.1", "Cookie": "secret=1"},
    )
    assert "ua=PlaywrightPrerender/" in r.text
    assert "internal=1" in r.text
    assert "cookie=\n" in r.text


# --- failure modes --------------------------------------------------------


async def test_timeout_passthrough_serves_the_shell(
    service_factory, client: httpx.AsyncClient
):
    svc = await service_factory(timeout_ms=800)
    r = await client.get(f"{svc}/never/")
    assert r.status_code == 200
    assert "Loading…" in r.text
    assert "<script" in r.text  # untouched origin body
    health = (await client.get(f"{svc}/health/")).json()
    assert "not satisfied" in health["last_error"]


async def test_timeout_snapshot_serves_partial_render(
    service_factory, client: httpx.AsyncClient
):
    svc = await service_factory(timeout_ms=800, on_timeout="snapshot")
    r = await client.get(f"{svc}/never/")
    assert r.status_code == 200
    assert "Partial content" in r.text
    assert "<script src" not in r.text


async def test_timeout_503(service_factory, client: httpx.AsyncClient):
    svc = await service_factory(timeout_ms=800, on_timeout="503")
    r = await client.get(f"{svc}/never/")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"


async def test_queue_full_takes_the_on_timeout_path(
    service_factory, client: httpx.AsyncClient
):
    svc = await service_factory(
        concurrency=1, queue_wait_ms=200, timeout_ms=5000, on_timeout="503"
    )
    # Arrival order over two connections isn't defined, so assert the
    # property: exactly one renders, the other is rejected while it waits.
    first, second = await asyncio.gather(
        client.get(f"{svc}/slow/"), client.get(f"{svc}/slow/")
    )
    assert sorted([first.status_code, second.status_code]) == [200, 503]
    rendered = first if first.status_code == 200 else second
    assert "Slow content" in rendered.text


# --- other wait modes and path rules -------------------------------------


async def test_wait_for_selector(service_factory, client: httpx.AsyncClient):
    svc = await service_factory(wait_for="selector:#ready")
    r = await client.get(f"{svc}/selector/")
    assert r.status_code == 200
    assert "selector content" in r.text


async def test_wait_for_networkidle(service_factory, client: httpx.AsyncClient):
    svc = await service_factory(wait_for="networkidle")
    r = await client.get(f"{svc}/never/")
    assert r.status_code == 200
    assert "Partial content" in r.text


async def test_wait_for_load(service_factory, client: httpx.AsyncClient):
    svc = await service_factory(wait_for="load")
    r = await client.get(f"{svc}/never/")
    assert r.status_code == 200
    assert "Partial content" in r.text


async def test_path_deny_and_allow(service_factory, client: httpx.AsyncClient):
    svc = await service_factory(
        path_deny=r"^/late/", path_allow=r"^/(late|)/?$"
    )
    denied = await client.get(f"{svc}/late/")
    assert "Loading…" in denied.text
    not_allowed = await client.get(f"{svc}/gone/")
    assert not_allowed.status_code == 200
    assert "Loading…" in not_allowed.text
    allowed = await client.get(f"{svc}/")
    assert "Hello from home" in allowed.text


async def test_asset_cache_can_be_disabled(
    service_factory, client: httpx.AsyncClient
):
    svc = await service_factory(asset_cache_mb=0)
    before = hits.get("/assets/app.js", 0)
    await client.get(f"{svc}/assets-count/")
    await client.get(f"{svc}/assets-count/")
    assert hits["/assets/app.js"] - before == 2
    assert (await client.get(f"{svc}/health/")).json()["asset_cache_bytes"] == 0


@pytest.mark.parametrize("engine", ["webkit", "firefox"])
async def test_other_engines_render(
    service_factory, client: httpx.AsyncClient, engine: str
):
    from pathlib import Path

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        executable = getattr(pw, engine).executable_path
    if not Path(executable).exists():  # noqa: ASYNC240 - test setup
        pytest.skip(
            f"{engine} not installed: run `playwright install {engine}`"
        )
    svc = await service_factory(browser_engine=engine)
    r = await client.get(f"{svc}/dead/")
    assert r.status_code == 404


async def test_unreachable_origin_is_a_502_not_a_traceback(
    service_factory, client: httpx.AsyncClient
):
    from .conftest import free_port

    svc = await service_factory(origin=f"http://127.0.0.1:{free_port()}")
    r = await client.get(f"{svc}/")
    assert r.status_code == 502
    health = (await client.get(f"{svc}/health/")).json()
    assert health["last_error"].startswith("probe: ")
