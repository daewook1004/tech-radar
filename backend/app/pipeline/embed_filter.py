import numpy as np
from fastembed import TextEmbedding
from sqlalchemy.orm import Session

from app.config import get_interests
from app.db.models import Content as ContentRow

# docs/ARCHITECTURE.md §7.1 — 다국어(한/영 혼합) 지원 + PyTorch 없이 가벼운 ONNX 런타임(fastembed) 사용.
# intfloat/multilingual-e5-small은 fastembed 번들 목록에 없어(large만 있음) 원안대로 MiniLM 사용.
_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_WEIGHT_BY_TIER = {"high": 1.0, "medium": 0.5, "low": 0.1}
_TOP_K = 50

# fastembed 기본값 256으로 두면 330건이 사실상 한 배치로 들어가서 어텐션 중간값이
# 폭증한다 — 실측 피크 1.7GB로 서버 RAM(909MB)의 거의 두 배였고, 스왑 폭주 끝에
# 2026-09-10 서버 전체가 멈췄다. 16으로 줄이면 피크가 0.5~0.7GB로 떨어지고
# 속도 차이는 거의 없다(로컬 31.8초 → 35.7초). 문서 수가 늘어도 배치 단위라
# 피크 메모리는 크게 늘지 않는다.
_BATCH_SIZE = 16

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model


def _category_texts() -> tuple[list[str], list[float]]:
    interests = get_interests()
    texts: list[str] = []
    weights: list[float] = []
    for tier, categories in interests.items():
        weight = _WEIGHT_BY_TIER.get(tier, 0.1)
        for cat in categories:
            texts.append(f"{cat['name']}: {cat['description']}")
            weights.append(weight)
    return texts, weights


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(np.dot(a, b) / denom)


def embed_filter(session: Session, rows: list[ContentRow], top_k: int = _TOP_K) -> list[ContentRow]:
    """관심사 카테고리 임베딩 대비 Relevance를 계산해 상위 top_k만 통과시킨다.
    비교 대상이 카테고리 10~15개뿐이라 pgvector 없이 in-memory로 충분(§ARCHITECTURE.md §1)."""
    if not rows:
        return []

    model = _get_model()
    category_texts, weights = _category_texts()
    category_embeddings = [np.array(e) for e in model.embed(category_texts)]

    doc_texts = [f"{r.title}\n{(r.text or '')[:1000]}" for r in rows]
    doc_embeddings = model.embed(doc_texts, batch_size=_BATCH_SIZE)

    for row, emb in zip(rows, doc_embeddings):
        emb = np.array(emb)
        weighted = [_cosine(emb, cat_emb) * w for cat_emb, w in zip(category_embeddings, weights)]
        relevance = max(weighted) if weighted else 0.0
        row.scores = {**(row.scores or {}), "relevance": round(relevance, 4)}

    rows_sorted = sorted(rows, key=lambda r: r.scores["relevance"], reverse=True)
    passed, rejected = rows_sorted[:top_k], rows_sorted[top_k:]

    for row in rejected:
        row.status = "filtered_out"
    session.commit()
    return passed
