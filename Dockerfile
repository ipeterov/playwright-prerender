# The official Playwright image ships Chromium, WebKit and Firefox matched
# to its Playwright release (in /ms-playwright, found via
# PLAYWRIGHT_BROWSERS_PATH), for both amd64 and arm64. `playwright` in
# pyproject.toml is pinned to the same version; keep the two in step.
#
# The image's own Python is Ubuntu's 3.12. This project wants 3.13, so uv
# installs a managed interpreter and builds the venv against that; the
# browsers don't care which Python drives them.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY playwright_prerender ./playwright_prerender
RUN uv python install 3.13 \
    && uv sync --locked --no-dev --no-editable \
    && rm -rf /root/.cache

ARG RELEASE=""
ENV RELEASE=$RELEASE
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health/" || exit 1

CMD ["playwright-prerender"]
