import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.config import get_interests, get_pipeline_settings
from app.db import repository
from app.db.models import Content as ContentRow

RankedItem = tuple[ContentRow, float, bool]  # (row, rrf_score, is_diversity_pick)

# 표준값(Elasticsearch/OpenSearch/Azure AI Search 등 하이브리드 서치의 RRF 기본값).
# 하위권 문서가 최종 순위에 미치는 영향을 완만하게 눌러주는 역할.
_RRF_K = 60

# relevance(임베딩, 0~1)와 importance/novelty/credibility(LLM 채점, 1~10)는 스케일이
# 전혀 달라서 예전처럼 원점수에 손으로 정한 가중치(40/3/2/1)를 곱해 더하면 스케일이
# 큰 신호가 은근슬쩍 유리해진다. RRF는 원점수 대신 "신호별 순위"만 쓰기 때문에
# 이 문제가 애초에 발생하지 않는다 — Elastic/OpenSearch 등이 벡터검색+키워드검색을
# 합칠 때 쓰는 표준 기법을 그대로 가져옴.
#
# 단, 동일 가중치로 4개 신호를 그냥 더하면 relevance가 "4표 중 1표"로만 취급돼서
# (예: 관심사와 거의 무관해도 importance가 최상위권이면 그 한 표로 순위가 크게 오름)
# 오히려 예전보다 관심사 매칭이 흐려지는 걸 실측으로 확인함(2026-08-26). relevance에
# 3배 가중치를 줘서 "관련 없으면 아무리 중요해도 밀린다"가 실제로 작동하게 함.
_SIGNAL_WEIGHTS = {"relevance": 3, "importance": 1, "novelty": 1, "credibility": 1}


def _rank_within(rows: list[ContentRow], key: str) -> dict[uuid.UUID, float]:
    """rows를 key 점수 내림차순으로 정렬해 1등부터 순위를 매긴다.
    점수가 없는 행(본문을 못 가져와 LLM 채점을 건너뛴 글)은 '낮음'이 아니라 '모름'이므로
    꼴찌가 아니라 점수 있는 행들의 가운데 순위를 준다 — 감점도 가점도 없이 다른 신호로 정해지게."""
    scored = [r for r in rows if key in (r.scores or {})]
    ordered = sorted(scored, key=lambda r: r.scores[key], reverse=True)
    ranks: dict[uuid.UUID, float] = {row.id: i + 1 for i, row in enumerate(ordered)}
    middle = (len(ordered) + 1) / 2
    for row in rows:
        ranks.setdefault(row.id, middle)
    return ranks


def rrf_scores(rows: list[ContentRow]) -> dict[uuid.UUID, float]:
    rank_maps = {key: _rank_within(rows, key) for key in _SIGNAL_WEIGHTS}
    return {
        row.id: sum(weight / (_RRF_K + rank_maps[key][row.id]) for key, weight in _SIGNAL_WEIGHTS.items())
        for row in rows
    }


def rank_and_cutoff(session: Session, rows: list[ContentRow], run_date: date) -> list[RankedItem]:
    """PRD §5.5 — RRF로 relevance/importance/novelty/credibility를 결합해 랭킹 +
    어제 노출 항목 제외 + 소스별 최대 개수 제한 + High 카테고리 최소 1개 다양성 보장.

    (구) score_threshold/min_items 기반 컷오프는 RRF 점수가 원점수와 스케일이 달라
    더 이상 의미가 없어 제거함 — RRF는 절대적 "좋음"이 아니라 오늘 후보군 내 상대
    순위이므로, 컷오프는 max_items_per_source(다양성)만으로 충분."""
    settings = get_pipeline_settings()["digest"]
    recent_ids = repository.get_recent_digest_content_ids(session, run_date, days=1)

    fresh_rows = [r for r in rows if r.id not in recent_ids]
    rrf = rrf_scores(fresh_rows)
    candidates = sorted(((r, rrf[r.id]) for r in fresh_rows), key=lambda pair: pair[1], reverse=True)

    max_items = settings["max_items"]
    max_per_source = settings.get("max_items_per_source")

    selected: list[RankedItem] = []
    source_counts: dict[str, int] = {}
    for r, s in candidates:
        if len(selected) >= max_items:
            break
        if max_per_source and source_counts.get(r.source, 0) >= max_per_source:
            continue  # 이 소스는 이미 한도만큼 들어감 — 점수는 높지만 건너뛰고 다른 소스에 자리를 내줌
        selected.append((r, s, False))
        source_counts[r.source] = source_counts.get(r.source, 0) + 1

    high_categories = {c["name"] for c in get_interests().get("high", [])}
    selected_topics = {(r.llm_analysis or {}).get("topic") for r, _, _ in selected}
    if not (selected_topics & high_categories):
        selected_ids = {r.id for r, _, _ in selected}
        for r, s in candidates:
            topic = (r.llm_analysis or {}).get("topic")
            if topic in high_categories and r.id not in selected_ids:
                if len(selected) >= max_items:
                    selected[-1] = (r, s, True)
                else:
                    selected.append((r, s, True))
                break

    selected = selected[:max_items]
    for row, _, _ in selected:
        row.status = "included"
    session.commit()
    return selected
