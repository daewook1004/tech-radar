from datetime import datetime, timedelta, timezone

import feedparser

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

# RSSCollector와 동일한 이유(일부 피드가 전체 아카이브를 반환하는 경우 대비)의 안전장치.
_LOOKBACK_DAYS = 7


class GeekNewsCollector(BaseCollector):
    source_name = "geeknews"

    def __init__(self, rss_url: str):
        self.rss_url = rss_url

    def collect(self) -> list[Content]:
        feed = feedparser.parse(self.rss_url)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"GeekNews RSS 파싱 실패: {feed.bozo_exception}")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_LOOKBACK_DAYS)
        items: list[Content] = []
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue

            published_at = _parse_time(entry)
            if published_at is not None and published_at < cutoff:
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
                    published_at=published_at,
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
