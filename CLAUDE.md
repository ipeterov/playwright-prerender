# CLAUDE.md

## What this is

A self-hosted dynamic-rendering service: a load balancer routes crawler
user agents here, the service opens the page in a real browser, waits until
the page says it's ready, strips scripts, and returns the HTML with the
status the page reported. `README.md` is the full spec and is kept
accurate; read it first. This file holds the reasoning behind decisions so
they don't get re-litigated by accident.

## Decisions already made

- **Passthrough on failure by default.** A renderer problem must never make
  things worse than not having a renderer. `503` exists but is opt-in.
- **First-value-wins status.** The ready flag is a setter installed before
  any page script runs. A 404 that then client-side-redirects to a listing
  stays a 404, otherwise every dead URL becomes a soft 404.
- **The origin probe decides what is renderable.** Only `200 text/html` is
  rendered; everything else passes through with its status and the small
  allowlist of headers. No path lists. `PATH_ALLOW`/`PATH_DENY` exist only
  for proxies that can't express a path rule.
- **Own user agent and `X-Prerender-Internal`, both**, for loop safety. The
  service fetches the app through the same public origin a browser uses.
- **`ORIGIN` is explicit**, never derived from the request `Host`. Deriving
  it would make the service render any site someone points it at.
- **No page cache; an in-memory asset cache.** Outputs bring invalidation
  problems and belong in the CDN. Inputs (content-hashed bundles) are safe
  to cache when the origin's `Cache-Control` says so. Documents are never
  cached.
- **Warm context pool sized to `CONCURRENCY`; contexts never reused.**
  Isolation stays absolute; only creation cost is amortised.
- **No image/font/media blocking.** Blocking triggers app fallbacks and puts
  markup in the snapshot a visitor never sees. Assets don't need to finish
  loading for a crawler to read the DOM.
- **DOM quiet after the wait condition.** It measures "the framework stopped
  committing" directly; network idle is a worse proxy and is only an
  explicit mode.
- **One browser per process, launched at startup; exit when it dies.** No
  relaunch, no idle close, no lazy launch. That machinery hides bugs;
  disposable containers make it unnecessary. `uvicorn` with one worker.
- **Chromium default, engine switchable.** Chromium for Googlebot parity;
  WebKit has fixed a CDP deadlock in another deployment of this pattern.
- **Prerender.io meta tags kept** as a second status source, so migrating
  apps need no changes.
- **Playwright pin equals the base image version.** Always bump the two
  together (see `DEVELOPMENT.md`).
- **Logs: text or JSON on stdout, plus an optional New Relic sink.** Every
  record carries `source=playwright-prerender`. Adding a sink is one
  `logging.Handler` subclass in `logs.py`; the record-to-dict step is shared.

## Working here

- Public repo, MIT. Keep the README honest and specific; no hype.
- Conventions follow the author's other Python projects: `uv`, hatchling,
  the ruff rule set in `pyproject.toml`, tests with pytest. Run
  `uv run ruff check . && uv run ruff format --check` and `uv run pytest`
  before calling anything done.
- Do not commit, stage or push. Flag a checkpoint with a suggested commit
  subject and the `Claude-Session:` trailer; the author commits from the
  IDE after reviewing the diff. Trunk-based on `main`, no branches.
- Do not use the structured multiple-choice question tool; ask in prose.
- Scratch files go in the session scratchpad directory, never `/tmp`.
- New behaviour goes with a case in `tests/stub_app` and an assertion in
  `tests/test_flow.py`. The stub app is also what
  `docker-compose.example.yml` serves, so keep it a coherent little site.
