from datetime import datetime, timezone

import feedparser
import requests

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

_API_URL = "http://export.arxiv.org/api/query"
_MAX_RESULTS = 50


class ArxivCollector(BaseCollector):
    source_name = "arxiv"

    def __init__(self, categories: list[str], keywords: list[str]):
        self.categories = categories
        self.keywords = keywords

    def collect(self) -> list[Content]:
        category_clause = " OR ".join(f"cat:{c}" for c in self.categories)
        keyword_clause = " OR ".join(f'abs:"{k}"' for k in self.keywords)
        search_query = f"({category_clause}) AND ({keyword_clause})"

        resp = requests.get(
            _API_URL,
            params={
                "search_query": search_query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": _MAX_RESULTS,
            },
            timeout=30,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)

        now = datetime.now(timezone.utc)
        items: list[Content] = []
        for entry in feed.entries:
            title = (entry.get("title") or "").strip().replace("\n", " ")
            url = entry.get("link") or entry.get("id")
            if not title or not url:
                continue

            tags = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
            items.append(
                Content(
                    source=self.source_name,
                    source_type="paper",
                    author=entry.get("author"),
                    title=title,
                    text=(entry.get("summary") or "").strip(),
                    url=url,
                    published_at=_parse_time(entry),
                    collected_at=now,
                    tags=tags,
                    raw_data=to_json_safe(dict(entry)),
                )
            )
        return items


def _parse_time(entry) -> datetime | None:
    value = entry.get("published_parsed")
    if value:
        return datetime(*value[:6], tzinfo=timezone.utc)
    return None
