from datetime import datetime, timezone

import feedparser

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content


class GeekNewsCollector(BaseCollector):
    source_name = "geeknews"

    def __init__(self, rss_url: str):
        self.rss_url = rss_url

    def collect(self) -> list[Content]:
        feed = feedparser.parse(self.rss_url)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"GeekNews RSS 파싱 실패: {feed.bozo_exception}")

        now = datetime.now(timezone.utc)
        items: list[Content] = []
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue
            text = entry.get("summary") or entry.get("description") or ""
            items.append(
                Content(
                    source=self.source_name,
                    source_type="community",
                    author=entry.get("author"),
                    title=title,
                    text=text,
                    url=url,
                    published_at=_parse_time(entry),
                    collected_at=now,
                    tags=[],
                    raw_data=to_json_safe(dict(entry)),
                )
            )
        return items


def _parse_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None
