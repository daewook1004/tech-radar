from difflib import SequenceMatcher

from app.schemas.content import Content

# PRD §5.2 — MVP는 URL 정규화 + 제목/본문 유사도 기반 "단순 중복 제거만".
# Cross-source Topic Clustering은 v2.
TITLE_SIMILARITY_THRESHOLD = 0.85


def dedup(items: list[Content]) -> list[Content]:
    seen_hashes: set[str] = set()
    unique: list[Content] = []
    for item in items:
        if item.content_hash in seen_hashes:
            continue
        if _is_near_duplicate(item, unique):
            continue
        seen_hashes.add(item.content_hash)
        unique.append(item)
    return unique


def _is_near_duplicate(item: Content, existing: list[Content]) -> bool:
    for other in existing:
        ratio = SequenceMatcher(None, item.title.lower(), other.title.lower()).ratio()
        if ratio >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False
