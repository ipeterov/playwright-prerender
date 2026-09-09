"""Where a request's time went, phase by phase, for the request log line.

One `Timeline` per request. Phases are marked as they end; the log fields are
the deltas between consecutive marks, so every millisecond of a request is
attributed to exactly one phase and the phases sum to the total. Marks that
never happen (a passthrough has no `flag`) simply produce no field.
"""

import time

# Phase name -> the mark that starts it. The order is the request's order.
PHASES: tuple[tuple[str, str, str], ...] = (
    # A slot in the concurrency semaphore.
    ("queue_ms", "received", "slot"),
    # A warm context from the pool (or a cold one if the pool was empty).
    ("context_ms", "slot", "context"),
    # The document request: navigation start until the origin's response
    # body is in hand. Origin time-to-last-byte, as seen by the browser.
    ("origin_ms", "context", "origin"),
    # The app booting: document received until the ready flag is raised.
    # Bundle parse and execution, its own API calls, and rendering.
    ("boot_ms", "origin", "flag"),
    # DOM quiet after the flag (0 when SETTLE_QUIET_MS is 0).
    ("settle_ms", "flag", "settled"),
    # Reading the flag value and serialising the DOM.
    ("extract_ms", "settled", "extracted"),
    # Script stripping and building the HTTP response.
    ("post_ms", "extracted", "done"),
)


class Timeline:
    def __init__(self) -> None:
        self._marks: dict[str, float] = {"received": time.monotonic()}

    def mark(self, name: str) -> None:
        self._marks.setdefault(name, time.monotonic())

    def has(self, name: str) -> bool:
        return name in self._marks

    def fields(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for field, start, end in PHASES:
            if start in self._marks and end in self._marks:
                out[field] = _ms(self._marks[end] - self._marks[start])
        now = time.monotonic()
        out["total_ms"] = _ms(now - self._marks["received"])
        return out


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class OriginRequests:
    """Counts the page's own requests back to the origin during a render:
    the API calls the app makes while booting. Documents and static assets
    are excluded (the former is the render itself, the latter are the asset
    cache's business)."""

    def __init__(self, origin_host: str) -> None:
        self.origin_host = origin_host
        self.count = 0
        self.total_ms = 0
        self.max_ms = 0

    def record(self, host: str, resource_type: str, duration_ms: float) -> None:
        if host != self.origin_host or resource_type not in ("fetch", "xhr"):
            return
        self.count += 1
        ms = round(duration_ms)
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)

    def fields(self) -> dict[str, int]:
        return {
            "api_requests": self.count,
            "api_total_ms": self.total_ms,
            "api_max_ms": self.max_ms,
        }
