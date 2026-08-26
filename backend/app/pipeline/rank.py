from datetime import date

from sqlalchemy.orm import Session

from app.config import get_interests, get_pipeline_settings
from app.db import repository
from app.db.models import Content as ContentRow

RankedItem = tuple[ContentRow, float, bool]  # (row, final_score, is_diversity_pick)


def _final_score(row: ContentRow) -> float:
    scores = row.scores or {}
    # relevance(0~1)*40 + importance/novelty/credibility(각 1~10) 가중합 → 0~100 스케일
    return (
        scores.get("relevance", 0) * 40
        + scores.get("importance", 0) * 3
        + scores.get("novelty", 0) * 2
        + scores.get("credibility", 0) * 1
    )


def rank_and_cutoff(session: Session, rows: list[ContentRow], run_date: date) -> list[RankedItem]:
    """PRD §5.5 — Score 임계치 컷오프 + 어제 노출 항목 제외 + High 카테고리 최소 1개 다양성 보장."""
    settings = get_pipeline_settings()["digest"]
    recent_ids = repository.get_recent_digest_content_ids(session, days=1)

    fresh_rows = [r for r in rows if r.id not in recent_ids]
    scored = sorted(((r, _final_score(r)) for r in fresh_rows), key=lambda pair: pair[1], reverse=True)

    above_threshold = [(r, s) for r, s in scored if s >= settings["score_threshold"]]
    candidates = above_threshold if len(above_threshold) >= settings["min_items"] else scored

    selected: list[RankedItem] = [(r, s, False) for r, s in candidates[: settings["max_items"]]]

    high_categories = {c["name"] for c in get_interests().get("high", [])}
    selected_topics = {(r.llm_analysis or {}).get("topic") for r, _, _ in selected}
    if not (selected_topics & high_categories):
        selected_ids = {r.id for r, _, _ in selected}
        for r, s in candidates:
            topic = (r.llm_analysis or {}).get("topic")
            if topic in high_categories and r.id not in selected_ids:
                if len(selected) >= settings["max_items"]:
                    selected[-1] = (r, s, True)
                else:
                    selected.append((r, s, True))
                break

    selected = selected[: settings["max_items"]]
    for row, _, _ in selected:
        row.status = "included"
    session.commit()
    return selected
