from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Content as ContentRow
from app.db.models import Digest, DigestItem, PipelineRun
from app.schemas.content import Content as ContentSchema


def upsert_content_batch(session: Session, items: list[ContentSchema]) -> list[ContentRow]:
    """content_hash가 이미 DB에 있는 글(다른 날 이미 수집됐다가 소스에 다시 노출된 것)은
    오늘 배치에서 조용히 걸러낸다. RETURNING은 실제로 새로 INSERT된 행만 돌려주므로,
    이미 존재하는 행은 자동으로 파이프라인 뒤 단계(재채점, "전체 목록" 노출 등)에서 빠진다.
    이전엔 SELECT ... WHERE content_hash IN (...)으로 기존 행까지 같이 반환해서,
    RSS 7일 lookback·HN 프론트 체류 등으로 같은 글이 매일 재수집될 때마다 LLM으로
    재채점되고 다이제스트 전체 목록에도 매일 반복 노출됐다(2026-09-03 실측)."""
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
    stmt = (
        pg_insert(ContentRow)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["content_hash"])
        .returning(ContentRow)
    )
    result = session.execute(stmt)
    session.commit()
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
    Content에는 run_date FK가 없어 collected_at 날짜로 근사한다 (수집·발송이 같은 날 배치이므로 정확).
    run_date가 KST 달력 날짜라 비교도 KST로 해야 한다 — 파이프라인은 KST 07:00(= UTC 전날 22:00)에
    도는데 DB 시간대가 UTC라, collected_at을 그대로 date()로 자르면 하루 이른 날짜가 나온다.
    그래서 상세 페이지마다 다음 날 수집분이 붙고 가장 최근 페이지는 비어 있었다
    (2026-09-04 KST 전환 이후, 2026-09-11 발견)."""
    collected_kst_date = func.date(func.timezone("Asia/Seoul", ContentRow.collected_at))
    stmt = select(ContentRow).where(collected_kst_date == run_date)
    return list(session.scalars(stmt).all())
