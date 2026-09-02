from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Content as ContentRow
from app.db.models import Digest, DigestItem, PipelineRun
from app.schemas.content import Content as ContentSchema


def upsert_content_batch(session: Session, items: list[ContentSchema]) -> list[ContentRow]:
    """중복(content_hash)은 DB 레벨에서도 안전망으로 무시하고, 최종적으로
    이번 배치에 해당하는 모든 행(신규 삽입 + 기존 존재분)을 DB 상태 그대로 반환한다."""
    if not items:
        return []

    rows = [
        dict(
            id=item.id,
            source=item.source,
            source_type=item.source_type,
            author=item.author,
            title=item.title,
            text=item.text,
            url=item.url,
            published_at=item.published_at,
            collected_at=item.collected_at,
            tags=item.tags,
            raw_data=item.raw_data,
            content_hash=item.content_hash,
            engagement_metrics=item.engagement_metrics,
            language=item.language,
            status=item.status,
        )
        for item in items
    ]
    stmt = pg_insert(ContentRow).values(rows).on_conflict_do_nothing(index_elements=["content_hash"])
    session.execute(stmt)
    session.commit()

    hashes = [item.content_hash for item in items]
    result = session.execute(select(ContentRow).where(ContentRow.content_hash.in_(hashes)))
    return list(result.scalars().all())


def start_pipeline_run(session: Session, run_date: date) -> PipelineRun:
    """같은 날 재실행(개발 중 반복 실행 포함)해도 안전하게 상태를 리셋."""
    existing = session.scalar(select(PipelineRun).where(PipelineRun.run_date == run_date))
    if existing:
        existing.started_at = datetime.utcnow()
        existing.finished_at = None
        existing.status = "running"
        existing.failures = []
        existing.stats = {}
        session.commit()
        return existing

    run = PipelineRun(
        run_date=run_date, started_at=datetime.utcnow(), status="running", failures=[], stats={}
    )
    session.add(run)
    session.commit()
    return run


def finish_pipeline_run(session: Session, run: PipelineRun, status: str, stats: dict) -> None:
    run.finished_at = datetime.utcnow()
    run.status = status
    run.stats = stats
    session.commit()


def get_recent_digest_content_ids(session: Session, run_date: date, days: int = 1) -> set:
    """run_date 기준 최근 N일(당일 제외)에 이미 포함됐던 content_id — freshness(어제 노출 제외) 판단용.
    date.today()가 아니라 run_date를 기준으로 삼고 Digest.run_date < run_date로 당일을 제외해야 한다 —
    안 그러면 같은 날 파이프라인이 재실행(수동 재시도, cron과 겹침 등)될 때 몇 분 전 자기 자신이 만든
    오늘자 digest 항목까지 '최근 노출'로 잘못 판단해 후보군이 부당하게 줄어든다 (2026-09-02 실측)."""
    cutoff = run_date - timedelta(days=days)
    stmt = (
        select(DigestItem.content_id)
        .join(Digest, Digest.id == DigestItem.digest_id)
        .where(Digest.run_date >= cutoff, Digest.run_date < run_date)
    )
    return set(session.scalars(stmt).all())


def save_digest(
    session: Session,
    run_date: date,
    trend_summary: str,
    items: list[tuple[ContentRow, float, bool]],
) -> Digest:
    digest = session.scalar(select(Digest).where(Digest.run_date == run_date))
    if digest:
        session.execute(delete(DigestItem).where(DigestItem.digest_id == digest.id))
        digest.generated_at = datetime.utcnow()
        digest.trend_summary = trend_summary
    else:
        digest = Digest(run_date=run_date, generated_at=datetime.utcnow(), trend_summary=trend_summary)
        session.add(digest)
        session.flush()  # digest.id 확보

    for rank, (row, score, is_diversity) in enumerate(items, start=1):
        session.add(
            DigestItem(
                digest_id=digest.id,
                content_id=row.id,
                rank=rank,
                final_score=score,
                is_diversity_pick=is_diversity,
            )
        )
    session.commit()
    return digest


def list_digests(session: Session) -> list[Digest]:
    stmt = select(Digest).order_by(Digest.run_date.desc())
    return list(session.scalars(stmt).all())


def get_digest_detail(
    session: Session, run_date: date
) -> tuple[Digest, list[tuple[DigestItem, ContentRow]]] | None:
    digest = session.scalar(select(Digest).where(Digest.run_date == run_date))
    if digest is None:
        return None

    stmt = (
        select(DigestItem, ContentRow)
        .join(ContentRow, ContentRow.id == DigestItem.content_id)
        .where(DigestItem.digest_id == digest.id)
        .order_by(DigestItem.rank)
    )
    items = [(item, content) for item, content in session.execute(stmt).all()]
    return digest, items


def get_content_by_date(session: Session, run_date: date) -> list[ContentRow]:
    """상세보기의 '전체 수집 목록' 섹션용 — 이메일의 _render_full_list와 동일한 정보.
    Content에는 run_date FK가 없어 collected_at 날짜로 근사한다 (수집·발송이 같은 날 배치이므로 정확)."""
    stmt = select(ContentRow).where(func.date(ContentRow.collected_at) == run_date)
    return list(session.scalars(stmt).all())
