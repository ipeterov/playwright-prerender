# playwright-prerender

A small, self-hosted dynamic-rendering service. It gives non-JavaScript
crawlers real HTML for a single-page app: a load balancer routes crawler
user agents to it, it opens the page in a real browser (Playwright), waits
until the page itself says it's ready, strips the scripts, and returns the
rendered HTML with the status the page reported. The app never knows the
service exists.

Google calls this pattern "dynamic rendering" and, since 2022, "a
workaround, not a recommended solution". Understood. It's for apps where
server-side rendering would mean restructuring the frontend.

I built it because the off-the-shelf options are gone: Rendertron is
archived, the open-source `prerender` server's repo is gone, and
browserless is SSPL. The official Playwright image is current and
multi-arch; this is a few hundred lines on top of it.

```
crawler ──► load balancer ──(User-Agent rule)──► playwright-prerender ──► app origin
browser ──► load balancer ──────────────────────────────────────────────► app origin
```

Runs as one container, `ghcr.io/ipeterov/playwright-prerender`, amd64 and
arm64. Needs one setting: `ORIGIN`.

## Try it

```bash
docker compose -f docker-compose.example.yml up --build
```

Then open `http://localhost:8090/late/` in a browser. That's the snapshot
of a page in the bundled stub app. `http://localhost:8090/dead/` shows a
404 that survives a client-side redirect, and `/robots.txt` is passed
through untouched.

Against your own app:

```bash
docker run -p 8090:8000 -e ORIGIN=https://example.com ghcr.io/ipeterov/playwright-prerender
curl -i http://localhost:8090/some/page/
```

## The contract with your app

Two window globals. Nothing else is asked of the app.

**`window.__prerenderReady`** (name configurable with `READY_FLAG`). Set it
from a page component once its real content is rendered, i.e. after its
data loaded. Setting it is the declaration "this page is meant for
crawlers". Accepted values:

- `true` → status 200.
- `{ status: 200 | 404 | 410 | 301, location?: string }` → that status;
  `location` only for 301.

**First value wins.** The service installs a setter before any page script
runs, so a page that reports 404 and then client-side-redirects to a listing
(which reports 200) stays a 404. Without this, every dead URL becomes a
"soft 404" of the listing page in Google's eyes.

**`window.__prerender = true`** (name configurable with `PRERENDER_FLAG`) is
set by the service before the page runs. Use it to skip analytics, chat
widgets, error reporting, and modal dialogs (age gates, cookie banners)
that would otherwise land in the snapshot. Optional.

```ts
// A React hook, for illustration.
export function useSeoReady(ready: boolean, status: 200 | 404 | 410 = 200) {
  useEffect(() => {
    if (ready) window.__prerenderReady = status === 200 ? true : { status };
  }, [ready, status]);
}
```

**Prerender.io compatibility.** `<meta name="prerender-status-code"
content="404">` and `<meta name="prerender-header" content="Location: /x">`
are honoured as a second status source when the flag is a bare `true` (or
when `WAIT_FOR` isn't `flag`), so apps migrating from prerender.io need no
changes.

## What it does with a request

**`GET /health/`** returns 200 with a JSON body: browser state, in-flight
renders, uptime, warm pool size, asset cache bytes, total renders, and the
last error. It never touches the origin. There is no unhealthy-but-alive
state: if the browser is gone, the process has exited.

**Any other `GET` or `HEAD`, any path:**

1. **Origin probe.** A plain HTTP fetch of `ORIGIN + path + query` with the
   service's own user agent, the header `X-Prerender-Internal: 1`, a fresh
   cookie jar, redirects not followed, and never the crawler's own cookies.
   If the response is anything other than `200` with `text/html`, it is
   **passed through unchanged**: status, body, `Content-Type`, `Location`,
   `X-Robots-Tag`, `Cache-Control`. This one rule makes `/robots.txt`,
   `/sitemap.xml`, permanent redirects and real 404s correct with no path
   list anywhere. `HEAD` requests, and any request carrying
   `X-Prerender-Internal`, stop here (loop guard).
2. **Render.** Take one of `CONCURRENCY` slots (or wait `QUEUE_WAIT_MS`, then
   take the `ON_TIMEOUT` path). Open a fresh browser context from the warm
   pool. Navigate. Wait per `WAIT_FOR`. Then wait for **DOM quiet**: no
   mutations for `SETTLE_QUIET_MS`, capped at `SETTLE_MAX_MS`. All of it
   inside `TIMEOUT_MS`.
3. **Status.** From the flag (or the meta tags). If the flag says 200 but the
   page's final URL differs from the requested one (the router redirected,
   e.g. `/docs/` → `/docs/intro/`), answer 301 to the final URL.
4. **Post-process.** Remove every reference to JavaScript: `<script>`
   tags except `application/ld+json`, `<link rel="modulepreload">`, and
   `<link rel="preload" as="script">`. Keep `<style>`, stylesheet, font and
   image links, so the snapshot still lays out when Google's Rich Results
   Test screenshots it. Copy `X-Robots-Tag` from the probe. Return
   `text/html; charset=utf-8`.

### `WAIT_FOR`: what "the page is done" means

- `flag` (default): the ready flag, as above. Strict and recommended.
- `selector:<css>`: an element matching the selector exists.
- `networkidle`: Playwright's 500 ms without requests. Works with any SPA
  out of the box; unreliable on pages that poll, lazy-load, or fire beacons.
- `load`: the window load event. Only useful for mostly-static pages.

DOM quiet runs after all of them. It's a direct measure of "the framework
stopped committing" and catches content that renders in a nested loading
boundary after the flag fired.

### `ON_TIMEOUT`: what to serve when it doesn't happen

- `passthrough` (default): the origin probe's body, i.e. the app's ordinary
  shell, exactly what the crawler got before this service existed. A
  renderer problem can never make things worse than not having one.
- `snapshot`: whatever rendered so far. Sensible with `WAIT_FOR=networkidle`
  on apps that have no flag.
- `503`: for setups that prefer the crawler to retry.

Every failure logs, and reports to Sentry if configured.

### Mobile and desktop

Google indexes with its smartphone crawler and renders at 412 px wide; its
desktop crawler renders at 1024 px. A responsive app whose JavaScript
changes the DOM by breakpoint (a hamburger menu instead of a nav bar, a
mobile-only layout) produces a different snapshot at each width, so the
service picks the viewport from the crawler's own user agent: a match on
`MOBILE_UA_REGEX` gets `MOBILE_VIEWPORT`, anything else gets `VIEWPORT`.
That reproduces what each Googlebot would have seen running the JavaScript
itself, which is the strongest position against being read as cloaking.

Both defaults are tall so content that only mounts when scrolled into view
still enters the DOM. Touch is not emulated in either mode, so a
`(pointer: coarse)` media query reports desktop. The request log line shows
which viewport a render got, e.g. `viewport=mobile:412x4096`.

Content parity between the two is the app's job: Google asks that the
mobile version carry the same primary content as desktop, and that is what
it ranks on.

### Not done on purpose

- **Images, fonts and media are not blocked.** Blocking them can trigger the
  app's own fallbacks and put markup in the snapshot a visitor never sees.
  They don't need to finish loading either: an `<img>` is in the DOM as soon
  as the framework renders it, and that's all a crawler reads.
- **No page cache.** Crawl volume on a normal site is small and a cache of
  outputs brings invalidation. Put one in the load balancer or CDN, keyed on
  the same crawler rule, if you need it.

## Speed

The target is a fast page rendering in about 200 ms on top of the app's own
render time, not seconds. Two mechanisms:

- **Warm context pool.** Creating a context and a blank page costs 20 to
  80 ms. A pool of `CONCURRENCY` pre-created contexts, each with the init
  script installed, is kept topped up in the background. A render takes
  one, uses it, discards it. Contexts are never reused across renders.
- **Asset cache.** A fresh context has an empty HTTP cache. The service
  intercepts requests for scripts, stylesheets and fonts and serves them
  from memory, honouring the origin's `Cache-Control` (`max-age` and
  `immutable`; never `no-store`, `private`, `no-cache`, or responses with
  `Set-Cookie`), bounded by `ASSET_CACHE_MB` with LRU eviction. It caches
  inputs, not outputs: documents are never cached. `ASSET_CACHE_MB=0`
  turns it off.

## Lifecycle and scaling

One browser per process, launched at startup and kept. If it dies, the
process exits non-zero and your orchestrator restarts the container. No
relaunch-after-N-renders, no idle close, no lazy launch. Memory growth is
the container memory limit's problem. Two consequences:

- With one replica, a restart means no healthy target for the 30 to 60
  seconds a new task takes; crawlers get 503 meanwhile. Google treats 503
  as temporary and retries. If it happens often enough to matter, run two
  replicas.
- A bad `BROWSER_ARGS` shows up as a crash loop at startup, not at first
  request.

`CONCURRENCY` renders at once, default 2. Roughly 100 to 200 MB per open
page plus the pool's idle pages and the asset cache: 1 vCPU / 2 GB → 2,
2 vCPU / 4 GB → 4. Scaling is horizontal and stateless: more instances in
the same target group.

## Configuration

Every variable has a matching CLI flag (`--origin`, `--wait-for`, ...);
`playwright-prerender --help` lists them.

| var | default | meaning |
|---|---|---|
| `ORIGIN` | required | scheme+host of the app, e.g. `https://example.com`. Never derived from the incoming `Host` |
| `PORT` | `8000` | listen port |
| `USER_AGENT` | `PlaywrightPrerender/<version> (+https://github.com/ipeterov/playwright-prerender)` | sent on every fetch. Must never match your crawler rule |
| `READY_FLAG` | `__prerenderReady` | window global the page sets |
| `PRERENDER_FLAG` | `__prerender` | window global the service sets |
| `WAIT_FOR` | `flag` | `flag` / `selector:<css>` / `networkidle` / `load` |
| `ON_TIMEOUT` | `passthrough` | `passthrough` / `snapshot` / `503` |
| `TIMEOUT_MS` | `12000` | whole render budget |
| `SETTLE_QUIET_MS` / `SETTLE_MAX_MS` | `300` / `2000` | DOM-quiet window and cap |
| `CONCURRENCY` | `2` | simultaneous renders; also the warm pool size |
| `QUEUE_WAIT_MS` | `3000` | wait for a slot before `ON_TIMEOUT` |
| `ASSET_CACHE_MB` | `256` | in-memory cache for static assets; `0` disables |
| `BLOCKED_HOSTS` | empty | comma list of hosts to abort requests to, e.g. `client.crisp.chat` |
| `DROP_HEADERS` | empty | extra passthrough headers to strip |
| `PATH_ALLOW` / `PATH_DENY` | empty | regexes on the path, for proxies that can't express a path rule |
| `BROWSER_ENGINE` | `chromium` | `chromium` / `webkit` / `firefox` |
| `BROWSER_ARGS` | empty | extra launch args, space-separated |
| `VIEWPORT` | `1024x4096` | viewport for desktop crawlers |
| `MOBILE_VIEWPORT` | `412x4096` | viewport for mobile crawlers |
| `MOBILE_UA_REGEX` | `\bMobile\b\|Android\|iPhone\|iPad` | crawler user agents matching this get `MOBILE_VIEWPORT`; empty disables |
| `LOG_FORMAT` | `text` | `text` / `json` |
| `SENTRY_DSN` | empty | optional |
| `RELEASE` | image version | Sentry release string |
| `NEW_RELIC_LICENSE_KEY` | empty | optional; also ship every log line to the New Relic Log API |
| `NEW_RELIC_LOG_ENDPOINT` | US endpoint | `https://log-api.eu.newrelic.com/log/v1` for EU accounts |

Fixed Chromium launch args: `--no-sandbox --disable-dev-shm-usage
--disable-gpu`. Container runtimes have a tiny `/dev/shm` and run as root.

**Engine note.** Chromium is the default for parity with Googlebot. In
another production deployment of this pattern, Chromium's DevTools protocol
deadlocked in `Target.createTarget` about 29 times a day across three
Chromium versions; WebKit, which Playwright drives over its own protocol,
fixed it at similar speed and memory. If you see that, the switch is
`BROWSER_ENGINE=webkit`.

## Logging

One line per request, to stdout:

```
2026-09-09T10:14:02.311Z INFO request source=playwright-prerender path=/characters/42/ engine=chromium wait_for=flag viewport=mobile:412x4096 outcome=rendered status=200 ms=214 asset_cache_hits=17 asset_cache_misses=0
```

`outcome` is one of `rendered`, `passthrough`, `timeout`, `queue_full`,
`error`. `LOG_FORMAT=json` emits the same fields as one JSON object per
line, which CloudWatch, Datadog, Loki and the New Relic agent all parse into
attributes without configuration. Startup logs the resolved config with
secrets masked. With `NEW_RELIC_LICENSE_KEY` set, every record is also
POSTed to the Log API, fire-and-forget, off the request path.

## Wiring

**Which crawlers.** Search engines: `Googlebot`, `bingbot`, `DuckDuckBot`,
`Applebot`, `YandexBot`. Social unfurlers (Slack, Discord, Twitter,
Facebook) only read `<head>`, which any server already renders, so routing
them here is pure load. SEO tools (Ahrefs, Semrush) crawl entire sites and
will queue. AI crawlers: your call. Never match a bare `bot`: it would catch
the service's own user agent.

**AWS ALB.** A listener rule above the default: `host-header <site>` +
`http-request-method GET,HEAD` + `http-header User-Agent` with the values
above (at most 128 chars per value, literal casing as the crawlers send it)
→ the service's target group. Health check `/health/`; deregistration delay
longer than `TIMEOUT_MS`. Add a debug rule, `http-header X-Prerender: 1` or
`query-string prerender=1` → the service, so `https://site/page/?prerender=1`
in a normal browser shows the snapshot.

**nginx.**

```nginx
map $http_user_agent $prerender {
    default 0;
    ~Googlebot 1;
    ~bingbot 1;
    ~DuckDuckBot 1;
    ~Applebot 1;
    ~YandexBot 1;
}

server {
    location / {
        if ($prerender) {
            proxy_pass http://prerender:8000;
        }
        proxy_set_header Host $host;
        # ... your normal app upstream
    }
}
```

## Local development

Native, against an app on port 8080:

```bash
uv run playwright-prerender --origin http://localhost:8080 --port 8090
```

Then open `http://localhost:8090/any/page/` in a browser. That's the
snapshot.

Docker on a Mac against servers on the host: the app's HTML references
`localhost:<port>`, which inside a container is the container itself.
Chromium can resolve that for us:

```bash
docker run -p 8090:8000 \
  -e ORIGIN=http://localhost:8080 \
  -e BROWSER_ARGS="--host-resolver-rules=MAP localhost host.docker.internal" \
  ghcr.io/ipeterov/playwright-prerender
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for tests and releases.

## License

MIT.
