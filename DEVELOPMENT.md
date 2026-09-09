# Development

For maintainers of `playwright-prerender`.

## Setup

```bash
uv sync --dev
uv run playwright install chromium     # add webkit / firefox to run those engine tests
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check
uv run pytest
```

`tests/test_flow.py` starts the stub app (`tests/stub_app`) and the service
in-process on free ports and drives a real Chromium; the whole suite takes
about 20 seconds. The WebKit and Firefox cases skip themselves when the
browser isn't installed locally; CI installs all three and runs them.

## Running locally

```bash
uv run python -m tests.stub_app --port 8080          # the fake SPA
uv run playwright-prerender --origin http://localhost:8080 --port 8090
open http://localhost:8090/late/
```

Or the same in Docker: `docker compose -f docker-compose.example.yml up --build`.

## Releasing

Releases are automated and driven by `version` in `pyproject.toml`:

1. Bump `version` (SemVer).
2. Push to `main`.

The release workflow runs CI, and if the tag `v<version>` doesn't exist yet
it builds the image for `linux/amd64` and `linux/arm64`, pushes it to
`ghcr.io/ipeterov/playwright-prerender:<version>` and `:latest`, creates the
tag, and publishes a GitHub Release with generated notes. A push that
doesn't bump the version runs CI only.

## Upgrading Playwright

The Docker base image and the `playwright` pin in `pyproject.toml` must be
the same version, because the image ships browsers matched to its
Playwright release. Bump both together, then:

```bash
uv lock
uv run playwright install chromium
uv run pytest
```

Available image tags: `https://mcr.microsoft.com/v2/playwright/python/tags/list`.
