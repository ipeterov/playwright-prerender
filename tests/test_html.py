from playwright_prerender.html import (
    MetaStatus,
    filter_headers,
    is_html,
    meta_status,
    path_and_query,
    strip_query_params,
    strip_scripts,
)
from playwright_prerender.render import resolve_status


def test_strip_scripts_keeps_ld_json_and_styles():
    html = (
        "<head><style>a{}</style><link rel=stylesheet href=/a.css>"
        '<script src="/app.js"></script>'
        '<SCRIPT type="application/ld+json">{"a":1}</SCRIPT>'
        "<script>\nmulti\nline\n</script ></head><body>x</body>"
    )
    out = strip_scripts(html)
    assert "<style>a{}</style>" in out
    assert "<link rel=stylesheet" in out
    assert '{"a":1}' in out
    assert "/app.js" not in out
    assert "multi" not in out


def test_strip_scripts_drops_script_preload_hints_only():
    html = (
        '<link rel="modulepreload" href="/chunk.js">'
        '<link rel="preload" as="script" href="/x.js">'
        '<link rel="preload" as="style" href="/x.css">'
        '<link rel="preload" as="font" href="/f.woff2" crossorigin>'
        "<link rel=stylesheet href=/a.css>"
        '<link rel="icon" href="/favicon.svg">'
    )
    out = strip_scripts(html)
    assert "modulepreload" not in out
    assert "/x.js" not in out
    assert 'as="style"' in out
    assert 'as="font"' in out
    assert "rel=stylesheet" in out
    assert 'rel="icon"' in out


def test_meta_status_reads_prerender_tags():
    html = (
        '<meta name="prerender-status-code" content="404">'
        "<meta name='prerender-header' content='Location: /x'>"
    )
    assert meta_status(html) == MetaStatus(status=404, location="/x")
    assert meta_status("<meta charset=utf-8>") == MetaStatus()


def test_filter_headers_keeps_only_passthrough_set():
    headers = [
        ("Content-Type", "text/plain"),
        ("Content-Length", "5"),
        ("Set-Cookie", "a=b"),
        ("X-Robots-Tag", "noindex"),
        ("Cache-Control", "max-age=60"),
        ("Location", "/new/"),
        ("Server-Timing", "x"),
        ("X-Custom", "y"),
    ]
    out = dict(filter_headers(headers))
    assert out == {
        "Content-Type": "text/plain",
        "X-Robots-Tag": "noindex",
        "Cache-Control": "max-age=60",
        "Location": "/new/",
    }
    assert "Cache-Control" not in dict(
        filter_headers(headers, extra_drop=("cache-control",))
    )


def test_is_html():
    assert is_html("text/html; charset=utf-8")
    assert is_html("TEXT/HTML")
    assert not is_html("application/json")
    assert not is_html(None)


def test_path_and_query():
    assert path_and_query("http://x/a/b/?q=1") == "/a/b/?q=1"
    assert path_and_query("http://x/a/") == "/a/"


def test_strip_query_params():
    names = ("prerender",)
    assert strip_query_params("/a/?prerender=1", names) == "/a/"
    assert strip_query_params("/a/?prerender=1&q=x", names) == "/a/?q=x"
    assert strip_query_params("/a/?q=x&prerender=1&r=y", names) == "/a/?q=x&r=y"
    assert strip_query_params("/a/?q=x", names) == "/a/?q=x"
    assert strip_query_params("/a/", names) == "/a/"
    assert strip_query_params("/a/?prerender=1", ()) == "/a/?prerender=1"


def test_resolve_status_flag_wins_then_meta_then_redirect_rule():
    none = MetaStatus()
    assert resolve_status(True, none, "/a/", "/a/") == (200, None)
    assert resolve_status({"status": 404}, none, "/a/", "/a/") == (404, None)
    assert resolve_status({"status": 410}, none, "/a/", "/a/") == (410, None)
    assert resolve_status(
        {"status": 301, "location": "/b/"}, none, "/a/", "/a/"
    ) == (301, "/b/")
    # 301 without a location is meaningless: treated as 200.
    assert resolve_status({"status": 301}, none, "/a/", "/a/") == (200, None)
    # Explicit flag status beats meta; bare true defers to meta.
    meta404 = MetaStatus(status=404)
    assert resolve_status({"status": 200}, meta404, "/a/", "/a/") == (200, None)
    assert resolve_status(True, meta404, "/a/", "/a/") == (404, None)
    # Router redirect: 200 but the URL moved.
    assert resolve_status(True, none, "/docs/", "/docs/intro/") == (
        301,
        "/docs/intro/",
    )
    # A 404 that then client-side-redirects stays a 404.
    assert resolve_status({"status": 404}, none, "/dead/", "/") == (404, None)
