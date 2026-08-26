from datetime import datetime, timedelta, timezone

import requests

from app.collectors.base import BaseCollector, to_json_safe
from app.schemas.content import Content

_API_BASE = "https://api.github.com"
_WATCH_RELEASE_LOOKBACK_DAYS = 3  # 파이프라인이 매일 도니 며칠 버퍼를 둬서 놓치는 release가 없게


class GitHubCollector(BaseCollector):
    """docs/ARCHITECTURE.md §8 스파이크 결론:
    - Watch Repo는 공식 API(Release/Star)로 정상 수집
    - Trending은 공식 엔드포인트가 없어 Search API 근사치(최근 생성 + star 정렬)로 대체"""

    source_name = "github"

    def __init__(
        self,
        watch_repos: list[str],
        trending_keywords: list[str],
        created_within_days: int,
        min_stars: int,
        github_token: str | None = None,
    ):
        self.watch_repos = watch_repos
        self.trending_keywords = trending_keywords
        self.created_within_days = created_within_days
        self.min_stars = min_stars
        self._headers = {"Accept": "application/vnd.github+json"}
        if github_token:
            self._headers["Authorization"] = f"Bearer {github_token}"

    def collect(self) -> list[Content]:
        items: list[Content] = []
        items.extend(self._collect_watch_releases())
        items.extend(self._collect_trending_approx())
        return items

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        return requests.get(url, headers=self._headers, params=params, timeout=15)

    def _collect_watch_releases(self) -> list[Content]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=_WATCH_RELEASE_LOOKBACK_DAYS)
        items: list[Content] = []

        for full_name in self.watch_repos:
            repo_resp = self._get(f"{_API_BASE}/repos/{full_name}")
            if repo_resp.status_code != 200:
                continue  # 저장소 하나가 rename/삭제돼도 나머지는 계속 진행
            star_count = repo_resp.json().get("stargazers_count", 0)

            releases_resp = self._get(f"{_API_BASE}/repos/{full_name}/releases", params={"per_page": 5})
            if releases_resp.status_code != 200:
                continue

            for release in releases_resp.json():
                if release.get("draft"):
                    continue
                published_at = _parse_iso(release.get("published_at"))
                if published_at is None or published_at < cutoff:
                    continue

                items.append(
                    Content(
                        source="github_watch",
                        source_type="code_repo",
                        author=full_name,
                        title=f"{full_name} {release.get('name') or release.get('tag_name')}",
                        text=release.get("body") or "",
                        url=release.get("html_url", f"https://github.com/{full_name}"),
                        published_at=published_at,
                        collected_at=now,
                        tags=[full_name],
                        raw_data=to_json_safe(release),
                        engagement_metrics={"stars": star_count},
                    )
                )
        return items

    def _collect_trending_approx(self) -> list[Content]:
        now = datetime.now(timezone.utc)
        created_after = (now - timedelta(days=self.created_within_days)).strftime("%Y-%m-%d")

        items: list[Content] = []
        seen_full_names: set[str] = set()
        for keyword in self.trending_keywords:
            query = f"{keyword} in:name,description,readme created:>{created_after} stars:>={self.min_stars}"
            resp = self._get(
                f"{_API_BASE}/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 15},
            )
            if resp.status_code != 200:
                continue  # 키워드 하나가 실패해도 나머지 키워드는 계속 진행

            for repo in resp.json().get("items", []):
                full_name = repo["full_name"]
                if full_name in seen_full_names:
                    continue
                seen_full_names.add(full_name)

                items.append(
                    Content(
                        source="github_trending_approx",
                        source_type="code_repo",
                        author=repo.get("owner", {}).get("login"),
                        title=full_name,
                        text=repo.get("description") or "",
                        url=repo.get("html_url", f"https://github.com/{full_name}"),
                        published_at=_parse_iso(repo.get("created_at")),
                        collected_at=now,
                        tags=[keyword],
                        raw_data=to_json_safe(repo),
                        engagement_metrics={"stars": repo.get("stargazers_count", 0)},
                    )
                )
        return items


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
