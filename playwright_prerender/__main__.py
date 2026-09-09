"""`python -m playwright_prerender` and the `playwright-prerender` console
script. One uvicorn worker: one event loop owns one async browser."""

import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_settings
from .logs import configure_logging


def main(argv: list[str] | None = None) -> None:
    try:
        settings = load_settings(argv)
    except ConfigError as e:
        print(f"playwright-prerender: {e}", file=sys.stderr)  # noqa: T201
        sys.exit(2)
    configure_logging(settings)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - a container service
        port=settings.port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
