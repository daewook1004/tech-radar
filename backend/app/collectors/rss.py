import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

logger = logging.getLogger(__name__)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# 일부 블로그(예: openai.com/blog/rss.xml)는 "최근 글"이 아니라 전체 아카이브를
# 그대로 피드에 담아 반환한다(2016년 글까지 수천 건). 매일 도는 배치라 이 window보다
# 오래된 글은 어차피 몇 번을 다시 수집해도 dedup으로 버려질 뿐이라 아예 걸러낸다.
# 날짜를 못 읽은 항목(퍼블리셔가 published/updated를 안 준 경우)은 안전하게 포함시킨다.
_LOOKBACK_DAYS = 7


def _slugify(name: str) -> str:
    return _NON_ALNUM_RE.sub("-", name.lower()).strip("-")


class RSSCollector(BaseCollector):
    """config/sources.yaml의 rss_blogs 목록(OpenAI/Anthropic/PyTorch Korea/TechBlogPosts 등)을
    순회하는 범용 어댑터. 피드 하나가 죽어도(URL 변경 등) 나머지는 계속 수집한다 —
    RSSCollector 전체가 run_pipeline의 한 "소스"이므로 내부적으로 더 세밀하게 격리해야
    블로그 7개 중 하나 때문에 전체가 실패하지 않는다."""

    source_name = "rss"

    def __init__(self, feeds: list[dict]):
        self.feeds = feeds  # [{"name": "OpenAI", "url": "https://..."}]

    def collect(self) -> list[Content]:
        now = datetime.now(timezone.utc)
        items: list[Content] = []

        for feed_cfg in self.feeds:
            name = feed_cfg["name"]
            url = feed_cfg["url"]
            try:
                items.extend(self._collect_one(name, url, now))
            except Exception:
                logger.exception("RSS feed '%s' (%s) 수집 실패, 건너뜀", name, url)
        return items

    def _collect_one(self, name: str, url: str, now: datetime) -> list[Content]:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"{name} RSS 파싱 실패: {feed.bozo_exception}")

        slug = _slugify(name)
        cutoff = now - timedelta(days=_LOOKBACK_DAYS)
        items: list[Content] = []
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            entry_url = (entry.get("link") or "").strip()
            if not title or not entry_url:
                continue

            published_at = _parse_time(entry)
            if published_at is not None and published_at < cutoff:
                continue

            text = entry.get("summary") or entry.get("description") or ""

            items.append(
                Content(
                    source=f"rss:{slug}",
                    source_type="blog",
                    author=entry.get("author") or name,
                    title=title,
                    text=text,
                    url=entry_url,
                    published_at=published_at,
                    collected_at=now,
                    tags=[name],
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
