"""Structured logging: one line per event, text or JSON on stdout, and an
optional New Relic sink that ships the same record fire-and-forget.

Every record carries `source=playwright-prerender` so the stream is
recognisable wherever it lands. Emit events with `log_event()`; the fields
travel on the record as `record.fields` and every sink renders them from
there, so adding a sink is one Handler subclass.
"""

import atexit
import datetime
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import sentry_sdk

from .config import LOG_SOURCE, Settings

NEW_RELIC_POST_TIMEOUT_S = 5

logger = logging.getLogger("playwright_prerender")


def log_event(level: int, event: str, **fields: Any) -> None:
    exc_info = fields.pop("exc_info", None)
    logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)


def _record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    ts = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
    out: dict[str, Any] = {
        "timestamp": ts.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "level": record.levelname,
        "source": LOG_SOURCE,
        "event": record.getMessage(),
    }
    for key, value in getattr(record, "fields", {}).items():
        out[key] = ",".join(value) if isinstance(value, tuple | list) else value
    if record.exc_info and record.exc_info[1] is not None:
        out.setdefault("error", repr(record.exc_info[1]))
    return out


def _quote(value: Any) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' "=\n'):
        return json.dumps(text)
    return text


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = _record_to_dict(record)
        head = (
            f"{data.pop('timestamp')} {data.pop('level')} {data.pop('event')}"
        )
        tail = " ".join(f"{k}={_quote(v)}" for k, v in data.items())
        return f"{head} {tail}".rstrip()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_dict(record), default=str)


class NewRelicHandler(logging.Handler):
    """POSTs each record to the New Relic Log API from a small thread pool.

    Never blocks the caller and never raises: a failed POST can only lose
    telemetry, and it is reported to Sentry under one fingerprint so an
    ingest outage is one issue rather than a storm.
    """

    def __init__(self, license_key: str, endpoint: str) -> None:
        super().__init__()
        self.license_key = license_key
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=NEW_RELIC_POST_TIMEOUT_S)
        self.pool = ThreadPoolExecutor(max_workers=2)
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed:
            # Something ran logging.shutdown() over us but left us attached.
            # Losing telemetry is fine; raising out of logger.log() is not.
            return
        data = _record_to_dict(record)
        envelope = {
            "timestamp": data["timestamp"],
            "message": json.dumps(data, default=str),
        }
        self.pool.submit(self._post, envelope)

    def _post(self, envelope: dict[str, str]) -> None:
        try:
            response = self.client.post(
                self.endpoint,
                headers={"X-License-Key": self.license_key},
                json=envelope,
            )
            response.raise_for_status()
        except Exception as e:  # noqa: BLE001
            with sentry_sdk.new_scope() as scope:
                scope.fingerprint = ["newrelic-post-failed"]
                sentry_sdk.capture_exception(e)

    def close(self) -> None:
        super().close()  # sets self._closed before the pool goes away
        self.pool.shutdown(wait=True)
        self.client.close()


def configure_logging(settings: Settings) -> None:
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(
        JsonFormatter() if settings.log_format == "json" else TextFormatter()
    )
    logger.addHandler(stdout)

    if settings.new_relic_license_key:
        logger.addHandler(
            NewRelicHandler(
                settings.new_relic_license_key, settings.new_relic_log_endpoint
            )
        )

    # uvicorn's access log duplicates our per-request line; keep its errors.
    logging.getLogger("uvicorn.access").disabled = True
