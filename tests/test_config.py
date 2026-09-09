import pytest

from playwright_prerender.config import ConfigError, Settings, load_settings


def test_load_settings_parses_types_from_env_and_flags():
    s = load_settings(
        ["--port", "9000", "--browser-args", "--a --b"],
        env={
            "ORIGIN": "https://x.com",
            "CONCURRENCY": "3",
            "BLOCKED_HOSTS": "a.com, b.com",
        },
    )
    assert s.port == 9000
    assert s.concurrency == 3
    assert s.blocked_hosts == ("a.com", "b.com")
    assert s.browser_args == ("--a", "--b")


def test_viewport_for_and_validation():
    s = Settings(origin="https://x.com")
    assert s.viewport_for(
        "Mozilla/5.0 (Linux; Android 6.0.1) Mobile Safari"
    ) == (
        "mobile",
        (412, 4096),
    )
    assert s.viewport_for("Mozilla/5.0 (compatible; bingbot/2.0)") == (
        "desktop",
        (1024, 4096),
    )
    assert s.viewport_for("") == ("desktop", (1024, 4096))
    with pytest.raises(ConfigError):
        Settings(origin="https://x.com", viewport="wide")
    with pytest.raises(ConfigError):
        Settings(origin="https://x.com", mobile_ua_regex="(")
