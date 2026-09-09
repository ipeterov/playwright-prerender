from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

app = FastAPI()

# Request counters, so tests can prove the asset cache served a hit and
# that the loop guard never asked the origin.
hits: dict[str, int] = {}

APP_JS = """
window.__appJsLoaded = (window.__appJsLoaded || 0) + 1;
"""


def page(title: str, script: str, *, head: str = "") -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
{head}
<link rel="stylesheet" href="/assets/app.css">
<script src="/assets/app.js"></script>
<script type="application/ld+json">{{"@type": "WebPage", "name": "{title}"}}</script>
</head>
<body>
<div id="root">Loading…</div>
<script>
const root = document.getElementById('root');
const setReady = (v) => {{ window.__prerenderReady = v; }};
{script}
</script>
</body>
</html>"""


ROUTES: dict[str, str] = {
    "/": page(
        "Home",
        "setTimeout(() => { root.textContent = 'Hello from home'; setReady(true); }, 50);",
    ),
    "/late/": page(
        "Late",
        """setTimeout(() => { root.textContent = 'Late content'; setReady(true);
             // A nested boundary that commits *after* the flag.
             setTimeout(() => { root.insertAdjacentHTML('beforeend', '<p id="nested">nested</p>'); }, 100);
           }, 100);""",
    ),
    "/dead/": page(
        "Dead",
        """root.textContent = 'Not found';
           setReady({ status: 404 });
           // The SPA then bounces to the listing, which reports 200.
           setTimeout(() => { history.pushState({}, '', '/'); root.textContent = 'Listing'; setReady(true); }, 50);""",
    ),
    "/gone/": page(
        "Gone", "root.textContent = 'Gone'; setReady({ status: 410 });"
    ),
    "/moved/": page(
        "Moved", "setReady({ status: 301, location: '/target/' });"
    ),
    "/router/": page(
        "Router",
        "history.replaceState({}, '', '/router/intro/'); root.textContent = 'Intro'; setReady(true);",
    ),
    "/never/": page("Never", "root.textContent = 'Partial content';"),
    "/slow/": page(
        "Slow",
        "setTimeout(() => { root.textContent = 'Slow content'; setReady(true); }, 3000);",
    ),
    "/meta404/": page(
        "Meta 404",
        "root.textContent = 'meta says gone'; setReady(true);",
        head='<meta name="prerender-status-code" content="404">',
    ),
    "/meta301/": page(
        "Meta 301",
        "setReady(true);",
        head='<meta name="prerender-status-code" content="301">'
        '<meta name="prerender-header" content="Location: /elsewhere/">',
    ),
    "/widgets/": page(
        "Widgets",
        """if (!window.__prerender) { root.insertAdjacentHTML('beforeend', '<div id="chat-widget">chat</div>'); }
           root.insertAdjacentHTML('beforeend', '<p>content</p>'); setReady(true);""",
    ),
    "/assets-count/": page(
        "Assets",
        "root.textContent = 'js loaded ' + window.__appJsLoaded; setReady(true);",
    ),
    "/blocked/": page(
        "Blocked",
        """const s = document.createElement('script'); s.src = 'http://blocked.invalid/x.js';
           s.onerror = () => { root.textContent = 'blocked script errored'; setReady(true); };
           s.onload = () => { root.textContent = 'blocked script loaded'; setReady(true); };
           document.head.appendChild(s);""",
    ),
    "/query/": page(
        "Query",
        "root.textContent = 'search=' + location.search; setReady(true);",
    ),
    "/responsive/": page(
        "Responsive",
        """const mobile = window.matchMedia('(max-width: 600px)').matches;
           root.innerHTML = mobile ? '<nav id="hamburger">menu</nav>' : '<nav id="navbar">Home Docs</nav>';
           root.insertAdjacentHTML('beforeend', '<p>' + innerWidth + 'x' + innerHeight + '</p>');
           setReady(true);""",
    ),
    "/selector/": page(
        "Selector",
        "setTimeout(() => { root.innerHTML = '<main id=\"ready\">selector content</main>'; }, 50);",
    ),
}


@app.get("/assets/app.js")
def app_js() -> Response:
    hits["/assets/app.js"] = hits.get("/assets/app.js", 0) + 1
    return Response(
        APP_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/assets/app.css")
def app_css() -> Response:
    hits["/assets/app.css"] = hits.get("/assets/app.css", 0) + 1
    return Response(
        "#root { color: rebeccapurple; }",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/robots.txt")
def robots() -> Response:
    return PlainTextResponse("User-agent: *\nDisallow: /private/\n")


@app.get("/old/")
def old() -> Response:
    return RedirectResponse("/new/", status_code=301)


@app.get("/private/")
def private() -> Response:
    return HTMLResponse(
        ROUTES["/"],
        headers={
            "X-Robots-Tag": "noarchive",
            "Set-Cookie": "session=abc; Path=/",
            "Server-Timing": "app;dur=1",
        },
    )


@app.get("/onboarding/")
def onboarding() -> Response:
    # A page that would take the full TIMEOUT_MS to give up on.
    return HTMLResponse(ROUTES["/never/"], headers={"X-Robots-Tag": "noindex"})


@app.get("/echo-headers/")
def echo_headers(request: Request) -> Response:
    ua = request.headers.get("user-agent", "")
    internal = request.headers.get("x-prerender-internal", "")
    cookie = request.headers.get("cookie", "")
    return PlainTextResponse(f"ua={ua}\ninternal={internal}\ncookie={cookie}\n")


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
def catch_all(path: str) -> Response:
    key = "/" + path
    hits[key] = hits.get(key, 0) + 1
    if key in ROUTES:
        return HTMLResponse(ROUTES[key])
    return HTMLResponse("<h1>Real 404</h1>", status_code=404)
