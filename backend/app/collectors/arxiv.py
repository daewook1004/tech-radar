import logging
import time
from datetime import datetime, timezone
from itertools import zip_longest

import feedparser
import requests

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

logger = logging.getLogger(__name__)

# 2026-09-14: export.arxiv.org/api/query가 어느 IP에서 요청하든 429 "Rate exceeded."를 돌려준다
# (운영 서버와 로컬 양쪽에서 확인, UA·https를 바꿔도 같음 — 우리 호출 빈도 문제가 아니라 arXiv API
# 쪽 사정이다). 같은 내용이 카테고리별 RSS로는 0.6초 만에 정상으로 오고 초록도 그대로 들어 있어
# (중앙값 1,495자, API가 주던 1,449자와 거의 같다) 수집 경로를 RSS로 옮겼다.
_RSS_URL = "https://rss.arxiv.org/rss/{category}"
_MAX_RESULTS = 50
_GAP_SEC = 3  # arXiv가 권하는 요청 간격
_TIMEOUT = 30
_HEADERS = {"User-Agent": "tech-radar/1.0 (+https://github.com/daewook1004/tech-radar)"}
# replace(개정판)는 이미 지난 논문이 다시 올라온 것이라 '오늘의 소식'이 아니다
_WANTED_TYPES = ("new", "cross")
_ABSTRACT_MARKER = "Abstract:"


def clean_abstract(summary: str) -> str:
    """RSS summary는 'arXiv:2609.11934v1 Announce Type: new  Abstract: ...' 형태로 온다."""
    _, marker, body = summary.partition(_ABSTRACT_MARKER)
    return (body if marker else summary).strip()


def is_wanted(entry: dict, keywords: list[str]) -> bool:
    """카테고리 피드는 하루 수백 건이라, 예전 API의 abs:"..." 조건을 여기서 대신 건다."""
    if not str(entry.get("arxiv_announce_type", "")).startswith(_WANTED_TYPES):
        return False
    blob = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    return any(keyword.lower() in blob for keyword in keywords)


def interleave(groups: list[list], limit: int) -> list:
    """카테고리별 목록에서 번갈아 하나씩 — 앞 카테고리가 상한을 다 차지하지 않게."""
    merged = [entry for row in zip_longest(*groups) for entry in row if entry is not None]
    return merged[:limit]


class ArxivCollector(BaseCollector):
    source_name = "arxiv"

    def __init__(self, categories: list[str], keywords: list[str]):
        self.categories = categories
        self.keywords = keywords

    def collect(self) -> list[Content]:
        seen: set[str] = set()
        groups: list[list] = []
        failures: list[str] = []

        for index, category in enumerate(self.categories):
            if index:
                time.sleep(_GAP_SEC)
            try:
                resp = requests.get(_RSS_URL.format(category=category), headers=_HEADERS, timeout=_TIMEOUT)
                resp.raise_for_status()
            except Exception as e:  # 카테고리 하나가 실패해도 나머지로 계속 — 소스 전체를 잃지 않게
                logger.warning("arxiv rss %s failed: %s", category, e)
                failures.append(category)
                continue

            picked = []
            for entry in feedparser.parse(resp.text).entries:
                key = entry.get("id") or entry.get("link")
                if not key or key in seen or not is_wanted(entry, self.keywords):
                    continue
                seen.add(key)  # 여러 카테고리에 걸린 논문은 한 번만
                picked.append(entry)
            groups.append(picked)

        if failures and not groups:
            raise RuntimeError(f"arxiv rss 전부 실패: {', '.join(failures)}")

        now = datetime.now(timezone.utc)
        items: list[Content] = []
        for entry in interleave(groups, _MAX_RESULTS):
            title = (entry.get("title") or "").strip().replace("\n", " ")
            url = entry.get("link") or entry.get("id")
            if not title or not url:
                continue

            items.append(
                Content(
                    source=self.source_name,
                    source_type="paper",
                    author=entry.get("author"),
                    title=title,
                    text=clean_abstract(entry.get("summary") or ""),
                    url=url,
                    published_at=_parse_time(entry),
                    collected_at=now,
                    tags=[t.get("term") for t in entry.get("tags", []) if t.get("term")],
                    raw_data=to_json_safe(dict(entry)),
                )
            )
        return items


def _parse_time(entry) -> datetime | None:
    value = entry.get("published_parsed")
    if value:
        return datetime(*value[:6], tzinfo=timezone.utc)
    return None
