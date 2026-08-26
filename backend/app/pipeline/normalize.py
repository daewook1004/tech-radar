import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.schemas.content import Content

_HANGUL_RE = re.compile(r"[가-힣]")
_DROP_QUERY_PREFIXES = ("utm_", "ref", "fbclid", "gclid", "igshid")


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not any(k.lower().startswith(p) for p in _DROP_QUERY_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def detect_language(text: str) -> str:
    return "ko" if _HANGUL_RE.search(text) else "en"


def compute_content_hash(url: str, title: str) -> str:
    basis = f"{url}|{title.strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalize(item: Content) -> Content:
    item.url = normalize_url(item.url)
    item.language = detect_language(f"{item.title} {item.text}")
    item.content_hash = compute_content_hash(item.url, item.title)
    return item
