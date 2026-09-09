"""The New Relic handler must survive uvicorn's startup, and never raise
out of a log call."""

import logging
import logging.config

import uvicorn

from playwright_prerender.config import Settings
from playwright_prerender.logs import (
    NewRelicHandler,
    configure_logging,
    log_event,
    logger,
)

# Nothing listens here; the POST fails on a pool thread and is swallowed.
DEAD_ENDPOINT = "http://127.0.0.1:9/log/v1"


def nr_settings() -> Settings:
    return Settings(
        origin="https://x.com",
        new_relic_license_key="test-key",
        new_relic_log_endpoint=DEAD_ENDPOINT,
    )


def nr_handler() -> NewRelicHandler:
    return next(h for h in logger.handlers if isinstance(h, NewRelicHandler))


def test_uvicorn_default_log_config_closes_our_handler():
    """Documents why __main__ passes log_config=None: dictConfig shuts
    down every existing handler but leaves it attached to our logger."""
    configure_logging(nr_settings())
    handler = nr_handler()
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    assert handler in logger.handlers
    assert handler._closed  # noqa: SLF001
    # And a closed handler drops the record instead of raising.
    log_event(logging.INFO, "after_shutdown")


def test_log_config_none_keeps_the_handler_alive():
    configure_logging(nr_settings())
    handler = nr_handler()
    uvicorn.Config(app=lambda *a: None, log_config=None)
    assert not handler._closed  # noqa: SLF001
    log_event(logging.INFO, "startup", key="value")
    handler.close()
