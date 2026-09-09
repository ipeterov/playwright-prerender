"""Configuration: environment variables, mirrored by CLI flags, into one
frozen settings object.

Every option has an env var and a `--flag`; the flag wins when both are
given. `ORIGIN` is the only required value, and it is never derived from the
incoming request (that would make the service render any site someone
points it at).
"""

import argparse
import dataclasses
import os
import re
from importlib.metadata import version

PACKAGE_VERSION = version("playwright-prerender")
DEFAULT_USER_AGENT = (
    f"PlaywrightPrerender/{PACKAGE_VERSION} "
    "(+https://github.com/ipeterov/playwright-prerender)"
)
INTERNAL_HEADER = "X-Prerender-Internal"
LOG_SOURCE = "playwright-prerender"

WAIT_FOR_MODES = ("flag", "networkidle", "load")  # plus "selector:<css>"
ON_TIMEOUT_MODES = ("passthrough", "snapshot", "503")
ENGINES = ("chromium", "webkit", "firefox")
LOG_FORMATS = ("text", "json")
# Googlebot Smartphone and bingbot's mobile variant both carry "Mobile".
DEFAULT_MOBILE_UA_REGEX = r"\bMobile\b|Android|iPhone|iPad"
_VIEWPORT_RE = re.compile(r"^(\d+)x(\d+)$")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Settings:
    origin: str
    port: int = 8000
    user_agent: str = DEFAULT_USER_AGENT
    ready_flag: str = "__prerenderReady"
    prerender_flag: str = "__prerender"
    wait_for: str = "flag"
    on_timeout: str = "passthrough"
    timeout_ms: int = 12000
    settle_quiet_ms: int = 300
    settle_max_ms: int = 2000
    concurrency: int = 2
    queue_wait_ms: int = 3000
    asset_cache_mb: int = 256
    blocked_hosts: tuple[str, ...] = ()
    drop_headers: tuple[str, ...] = ()
    path_allow: str = ""
    path_deny: str = ""
    # Query parameters removed from the URL before it is fetched, so a proxy
    # rule keyed on one of them (`?prerender=1` -> this service) can't match
    # the service's own fetches and loop. Headers don't need this: the
    # service never forwards the crawler's headers.
    strip_query_params: tuple[str, ...] = ("prerender",)
    browser_engine: str = "chromium"
    browser_args: tuple[str, ...] = ()
    # Google renders desktop at 1024 wide and smartphone at 412 wide. Tall,
    # so content gated behind "is it in view" checks still enters the DOM.
    viewport: str = "1024x4096"
    mobile_viewport: str = "412x4096"
    mobile_ua_regex: str = DEFAULT_MOBILE_UA_REGEX
    sentry_dsn: str = ""
    release: str = ""
    log_format: str = "text"
    new_relic_license_key: str = ""
    new_relic_log_endpoint: str = "https://log-api.newrelic.com/log/v1"

    def __post_init__(self) -> None:
        if not re.match(r"^https?://[^/]+$", self.origin):
            raise ConfigError(
                "ORIGIN must be scheme+host with no path, "
                f"e.g. https://example.com (got {self.origin!r})"
            )
        if self.wait_for not in WAIT_FOR_MODES and not self.wait_for.startswith(
            "selector:"
        ):
            raise ConfigError(
                f"WAIT_FOR must be one of {WAIT_FOR_MODES} or selector:<css>"
            )
        if self.on_timeout not in ON_TIMEOUT_MODES:
            raise ConfigError(f"ON_TIMEOUT must be one of {ON_TIMEOUT_MODES}")
        if self.browser_engine not in ENGINES:
            raise ConfigError(f"BROWSER_ENGINE must be one of {ENGINES}")
        if self.log_format not in LOG_FORMATS:
            raise ConfigError(f"LOG_FORMAT must be one of {LOG_FORMATS}")
        if self.concurrency < 1:
            raise ConfigError("CONCURRENCY must be at least 1")
        for name in ("path_allow", "path_deny", "mobile_ua_regex"):
            pattern = getattr(self, name)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ConfigError(
                        f"{name.upper()} is not a valid regex: {e}"
                    ) from e
        for name in ("viewport", "mobile_viewport"):
            if not _VIEWPORT_RE.match(getattr(self, name)):
                raise ConfigError(
                    f"{name.upper()} must look like WIDTHxHEIGHT, e.g. 1024x4096"
                )

    def viewport_for(self, user_agent: str) -> tuple[str, tuple[int, int]]:
        """('mobile' | 'desktop', (width, height)) for a crawler's UA."""
        if self.mobile_ua_regex and re.search(self.mobile_ua_regex, user_agent):
            return "mobile", _parse_viewport(self.mobile_viewport)
        return "desktop", _parse_viewport(self.viewport)

    @property
    def wait_selector(self) -> str | None:
        if self.wait_for.startswith("selector:"):
            return self.wait_for.removeprefix("selector:")
        return None

    def masked(self) -> dict[str, object]:
        """The resolved config with secrets masked, for the startup log."""
        out = dataclasses.asdict(self)
        for key in ("sentry_dsn", "new_relic_license_key"):
            if out[key]:
                out[key] = "***"
        return out


def _parse_viewport(value: str) -> tuple[int, int]:
    m = _VIEWPORT_RE.match(value)
    assert m is not None  # validated in __post_init__
    return int(m.group(1)), int(m.group(2))


# (field, env var, help). Types come from the dataclass field annotations.
_OPTIONS: list[tuple[str, str, str]] = [
    ("origin", "ORIGIN", "scheme+host of the app, e.g. https://example.com"),
    ("port", "PORT", "listen port"),
    ("user_agent", "USER_AGENT", "user agent sent on every fetch"),
    ("ready_flag", "READY_FLAG", "window global the page sets when ready"),
    ("prerender_flag", "PRERENDER_FLAG", "window global the service sets"),
    ("wait_for", "WAIT_FOR", "flag / selector:<css> / networkidle / load"),
    ("on_timeout", "ON_TIMEOUT", "passthrough / snapshot / 503"),
    ("timeout_ms", "TIMEOUT_MS", "whole render budget"),
    ("settle_quiet_ms", "SETTLE_QUIET_MS", "DOM-quiet window"),
    ("settle_max_ms", "SETTLE_MAX_MS", "DOM-quiet cap"),
    ("concurrency", "CONCURRENCY", "simultaneous renders and warm pool size"),
    ("queue_wait_ms", "QUEUE_WAIT_MS", "wait for a slot before ON_TIMEOUT"),
    (
        "asset_cache_mb",
        "ASSET_CACHE_MB",
        "in-memory static asset cache; 0 disables",
    ),
    (
        "blocked_hosts",
        "BLOCKED_HOSTS",
        "comma list of hosts to abort requests to",
    ),
    ("drop_headers", "DROP_HEADERS", "extra passthrough headers to strip"),
    ("path_allow", "PATH_ALLOW", "regex; only matching paths are rendered"),
    ("path_deny", "PATH_DENY", "regex; matching paths are never rendered"),
    (
        "strip_query_params",
        "STRIP_QUERY_PARAMS",
        "comma list of query parameters dropped before fetching the origin",
    ),
    ("browser_engine", "BROWSER_ENGINE", "chromium / webkit / firefox"),
    ("browser_args", "BROWSER_ARGS", "extra launch args, space-separated"),
    ("viewport", "VIEWPORT", "WIDTHxHEIGHT for desktop crawlers"),
    ("mobile_viewport", "MOBILE_VIEWPORT", "WIDTHxHEIGHT for mobile crawlers"),
    (
        "mobile_ua_regex",
        "MOBILE_UA_REGEX",
        "crawler UAs matching this get MOBILE_VIEWPORT; empty disables",
    ),
    ("sentry_dsn", "SENTRY_DSN", "optional Sentry DSN"),
    ("release", "RELEASE", "Sentry release string"),
    ("log_format", "LOG_FORMAT", "text / json"),
    (
        "new_relic_license_key",
        "NEW_RELIC_LICENSE_KEY",
        "optional; also ship logs to New Relic",
    ),
    (
        "new_relic_log_endpoint",
        "NEW_RELIC_LOG_ENDPOINT",
        "New Relic Log API endpoint (EU: https://log-api.eu.newrelic.com/log/v1)",
    ),
]

_FIELD_TYPES = {f.name: f.type for f in dataclasses.fields(Settings)}
_DEFAULTS = {
    f.name: f.default
    for f in dataclasses.fields(Settings)
    if f.default is not dataclasses.MISSING
}


def _parse_value(name: str, raw: str) -> object:
    kind = _FIELD_TYPES[name]
    if kind is int:
        try:
            return int(raw)
        except ValueError as e:
            raise ConfigError(f"{name.upper()} must be an integer") from e
    if kind == tuple[str, ...]:
        sep = " " if name == "browser_args" else ","
        return tuple(part.strip() for part in raw.split(sep) if part.strip())
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="playwright-prerender",
        description="Dynamic rendering service for single-page apps.",
    )
    for name, env, help_text in _OPTIONS:
        flag = "--" + name.replace("_", "-")
        default = _DEFAULTS.get(name, "required")
        parser.add_argument(
            flag,
            dest=name,
            default=None,
            help=f"{help_text} [env {env}, default {default!r}]",
        )
    return parser


def load_settings(
    argv: list[str] | None = None, env: dict[str, str] | None = None
) -> Settings:
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    values: dict[str, object] = {}
    for name, env_var, _ in _OPTIONS:
        raw = getattr(args, name)
        if raw is None:
            raw = env.get(env_var)
        if raw is None or raw == "":
            continue
        values[name] = _parse_value(name, raw)
    if "origin" not in values:
        raise ConfigError("ORIGIN is required (env ORIGIN or --origin)")
    return Settings(**values)  # type: ignore[arg-type]
