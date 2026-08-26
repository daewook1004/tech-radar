from datetime import datetime, timezone

import requests

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

_BASE_URL = "https://hacker-news.firebaseio.com/v0"
_TOP_STORIES_LIMIT = 150  # topstories 중 앞에서부터 이만큼만 상세 조회 (min_points로 추가 필터링)


class HackerNewsCollector(BaseCollector):
    source_name = "hackernews"

    def __init__(self, min_points: int):
        self.min_points = min_points

    def collect(self) -> list[Content]:
        story_ids = requests.get(f"{_BASE_URL}/topstories.json", timeout=15).json()
        story_ids = story_ids[:_TOP_STORIES_LIMIT]

        now = datetime.now(timezone.utc)
        items: list[Content] = []
        for story_id in story_ids:
            item = requests.get(f"{_BASE_URL}/item/{story_id}.json", timeout=15).json()
            if not item or item.get("type") != "story":
                continue
            if item.get("score", 0) < self.min_points:
                continue

            title = (item.get("title") or "").strip()
            if not title:
                continue
            # Ask HN/Show HN처럼 외부 url이 없는 글은 HN 자체 댓글 페이지를 url로 사용
            url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"

            items.append(
                Content(
                    source=self.source_name,
                    source_type="community",
                    author=item.get("by"),
                    title=title,
                    text=item.get("text", ""),
                    url=url,
                    published_at=datetime.fromtimestamp(item["time"], tz=timezone.utc) if item.get("time") else None,
                    collected_at=now,
                    tags=[],
                    raw_data=to_json_safe(item),
                    engagement_metrics={"points": item.get("score", 0), "comments": item.get("descendants", 0)},
                )
            )
        return items
