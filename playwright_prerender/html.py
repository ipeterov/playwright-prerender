"""Pure helpers: script stripping, header filtering, and the Prerender.io
meta-tag status. No heavy imports so tests stay instant."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

_SCRIPT_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>.*?</script\s*>", re.IGNORECASE | re.DOTALL
)
_TYPE_RE = re.compile(
    r"""\btype\s*=\s*["']?\s*application/ld\+json\s*["']?""", re.IGNORECASE
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""\b(?P<name>[\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)

# Hop-by-hop and stale headers that never survive passthrough.
DROP_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "set-cookie",
        "server-timing",
        "keep-alive",
    }
)

# The only headers copied from the origin onto a passthrough response.
PASSTHROUGH_HEADERS = (
    "content-type",
    "location",
    "x-robots-tag",
    "cache-control",
)


def strip_scripts(html: str) -> str:
    """Remove every reference to JavaScript: <script> tags (except JSON-LD)
    and the <link> hints that would make a client download scripts anyway
    (modulepreload, preload as=script). Stylesheet, font and image links
    stay so the snapshot still lays out."""

    def keep_or_drop_script(match: re.Match[str]) -> str:
        if _TYPE_RE.search(match.group("attrs")):
            return match.group(0)
        return ""

    def keep_or_drop_link(match: re.Match[str]) -> str:
        attrs = _attrs(match.group(0))
        rel = attrs.get("rel", "").lower().split()
        if "modulepreload" in rel:
            return ""
        if "preload" in rel and attrs.get("as", "").lower() == "script":
            return ""
        return match.group(0)

    html = _SCRIPT_RE.sub(keep_or_drop_script, html)
    return _LINK_RE.sub(keep_or_drop_link, html)


def _attrs(tag: str) -> dict[str, str]:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        value = next(v for v in m.groups()[1:] if v is not None)
        out[m.group("name").lower()] = value
    return out


@dataclass(frozen=True)
class MetaStatus:
    status: int | None = None
    location: str | None = None


def meta_status(html: str) -> MetaStatus:
    """Prerender.io compatibility: <meta name="prerender-status-code"> and
    <meta name="prerender-header" content="Location: /x">."""
    status = None
    location = None
    for tag in _META_RE.findall(html):
        attrs = _attrs(tag)
        name = attrs.get("name", "").lower()
        content = attrs.get("content", "").strip()
        if name == "prerender-status-code" and content.isdigit():
            status = int(content)
        elif name == "prerender-header":
            header, _, value = content.partition(":")
            if header.strip().lower() == "location" and value.strip():
                location = value.strip()
    return MetaStatus(status=status, location=location)


def filter_headers(
    headers: list[tuple[str, str]], extra_drop: tuple[str, ...] = ()
) -> list[tuple[str, str]]:
    """Headers to copy from an origin response onto a passthrough."""
    drop = DROP_HEADERS | {h.lower() for h in extra_drop}
    return [
        (name, value)
        for name, value in headers
        if name.lower() in PASSTHROUGH_HEADERS and name.lower() not in drop
    ]


def is_html(content_type: str | None) -> bool:
    return (
        bool(content_type)
        and content_type.split(";")[0].strip().lower() == "text/html"
    )


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def strip_query_params(path_and_query: str, names: tuple[str, ...]) -> str:
    """`path_and_query` without the named query parameters.

    The rest of the query string is kept in order; a query left empty is
    dropped with its `?`. Used on everything the service fetches so a proxy
    rule keyed on one of these parameters can't match the fetch and loop.
    """
    if not names or "?" not in path_and_query:
        return path_and_query
    path, _, query = path_and_query.partition("?")
    kept = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if k not in names
    ]
    return path + (f"?{urlencode(kept)}" if kept else "")
