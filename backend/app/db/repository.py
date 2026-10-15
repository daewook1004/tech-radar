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


def get_unprocessed_content(session: Session, collected_since: datetime) -> list[ContentRow]:
    """이전 실행이 저장까지만 하고 끝내지 못한 글 — 다음 실행이 이어서 처리하게 한다.
    upsert_content_batch()는 새로 INSERT된 행만 돌려주므로, 실행이 저장 직후 죽으면 그 행들은
    status='collected'로 남은 채 다음 날엔 '이미 있는 글'로 걸러져 영영 처리되지 않았다
    (2026-09-10 서버 정지로 199건이 이렇게 남음). 끝까지 처리된 행은 모두 filtered_out 이후
    상태로 넘어가므로, collected로 남은 행은 곧 처리가 중간에 끊긴 흔적이다."""
    stmt = select(ContentRow).where(
        ContentRow.status == "collected",
        ContentRow.collected_at >= collected_since,
    )
    return list(session.scalars(stmt).all())


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


def get_recent_pipeline_runs(session: Session, limit: int) -> list[PipelineRun]:
    stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def get_digest(session: Session, run_date: date) -> Digest | None:
    return session.scalar(select(Digest).where(Digest.run_date == run_date))


def save_digest(
    session: Session,
    run_date: date,
    trend_summary: str,
    items: list[tuple[ContentRow, float, bool]],
) -> Digest:
    digest = get_digest(session, run_date)
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
    digest = get_digest(session, run_date)
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


def get_corpus_for_ask(session: Session, body_chars: int) -> list:
    """/ask 질의용 — 수집한 글 전체를 수집 시각 오름차순으로.

    ORM 객체 대신 필요한 컬럼만, 본문은 SQL에서 잘라서 가져온다. 대시보드 프로세스는
    평소 30MB도 안 쓰는데 여기서 4,600건을 통째로 올리면 그 균형이 깨진다 —
    야간 파이프라인(피크 500MB)과 같은 1GB 호스트를 쓰므로 여유를 남겨야 한다."""
    stmt = select(
        ContentRow.id,
        ContentRow.source,
        ContentRow.title,
        ContentRow.url,
        ContentRow.collected_at,
        func.left(func.coalesce(ContentRow.text, ""), body_chars).label("text"),
    ).order_by(ContentRow.collected_at)
    return list(session.execute(stmt).all())


# --- 주제 대시보드 -----------------------------------------------------------
# 라벨은 topic_label.py가 제목만 보고 매긴 것(scores['topic'])이다. 처음에는 embed_filter가
# 이미 계산해 둔 카테고리 argmax를 공짜로 쓰려 했지만, LLM이 주제를 매긴 809건과 비교해
# 일치율이 35%여서 버렸다(근거는 topic_label.py 머리말과 docs/eval/topic-labels/).
# 라벨이 아직 없는 글(백필 전, 또는 그날 라벨 단계가 실패한 글)은 '기타'로 묶인다.
OTHER_LABEL = "기타"


def _kst_day():
    """수집일은 KST 달력 날짜로 — run_date와 같은 기준이어야 주/일 집계가 어긋나지 않는다."""
    return func.date(func.timezone("Asia/Seoul", ContentRow.collected_at))


def _label():
    """라벨이 있는 글은 그 주제로, 없는 글은 '기타'."""
    name = func.nullif(func.coalesce(ContentRow.scores["topic"].as_string(), ""), "")
    return func.coalesce(name, OTHER_LABEL)


def topic_weeks(session: Session, weeks: int = 8) -> list:
    """주제 × 주 히트맵용 — (주 시작일, 주제, 건수).

    SELECT와 GROUP BY에 같은 표현식 객체를 넘겨야 한다 — _label()을 두 번 부르면
    바인드 파라미터 이름이 달라져서 PostgreSQL이 같은 식으로 보지 않는다(GroupingError)."""
    day = _kst_day()
    label = _label()
    week = func.date(func.date_trunc("week", day))
    since = func.date(func.date_trunc("week", func.timezone("Asia/Seoul", func.now()))) - (weeks - 1) * 7
    stmt = (
        select(week.label("week"), label.label("topic"), func.count().label("n"))
        .where(day >= since)
        .group_by(week, label)
        .order_by(week, func.count().desc())
    )
    return list(session.execute(stmt).all())


def topic_trend(session: Session, days: int = 7) -> list:
    """급상승·급감용 — 최근 days일과 그 앞 days일의 주제별 건수를 나란히."""
    day = _kst_day()
    label = _label()
    today = func.date(func.timezone("Asia/Seoul", func.now()))
    recent = func.count().filter(day > today - days)
    previous = func.count().filter(day <= today - days)
    stmt = (
        select(label.label("topic"), recent.label("recent"), previous.label("previous"))
        .where(day > today - 2 * days)
        .group_by(label)
    )
    return list(session.execute(stmt).all())


def topic_blindspots(session: Session, days: int = 14) -> list:
    """사각지대용 — 주제별로 '수집된 건수'와 '다이제스트에 실린 건수'를 비교한다.
    많이 들어오는데 한 번도 안 실린 주제가 관심사 설정이나 랭킹이 놓치는 자리다."""
    published = select(DigestItem.content_id).scalar_subquery()
    label = _label()
    stmt = (
        select(
            label.label("topic"),
            func.count().label("collected"),
            func.count().filter(ContentRow.id.in_(published)).label("published"),
            func.count().filter(ContentRow.status != "filtered_out").label("passed"),
        )
        .where(_kst_day() > func.date(func.timezone("Asia/Seoul", func.now())) - days)
        .group_by(label)
        .order_by(func.count().desc())
    )
    return list(session.execute(stmt).all())


def collection_days(session: Session, days: int = 14) -> list:
    """날짜별 수집량 — (날짜, 건수, 다이제스트 게재 건수)."""
    published = select(DigestItem.content_id).scalar_subquery()
    day = _kst_day()
    stmt = (
        select(
            day.label("day"),
            func.count().label("collected"),
            func.count().filter(ContentRow.id.in_(published)).label("published"),
        )
        .where(day > func.date(func.timezone("Asia/Seoul", func.now())) - days)
        .group_by(day)
        .order_by(day)
    )
    return list(session.execute(stmt).all())


def archive_page(
    session: Session,
    topic: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list, int]:
    """아카이브 목록 한 페이지와 전체 건수.

    ORM 객체가 아니라 필요한 컬럼만 가져온다 — 6,500건을 통째로 올리면 대시보드
    프로세스가 야간 파이프라인과 같은 1GB 호스트에서 자리를 잃는다(get_corpus_for_ask와 같은 이유)."""
    published = select(DigestItem.content_id).scalar_subquery()
    filters = []
    if topic == OTHER_LABEL:
        filters.append(func.coalesce(ContentRow.scores["topic"].as_string(), "").in_(("", OTHER_LABEL)))
    elif topic:
        filters.append(ContentRow.scores["topic"].as_string() == topic)
    if source:
        filters.append(ContentRow.source == source)
    if q:
        filters.append(ContentRow.title.ilike(f"%{q}%"))

    total = session.execute(select(func.count()).select_from(ContentRow).where(*filters)).scalar_one()
    stmt = (
        select(
            ContentRow.id,
            ContentRow.source,
            ContentRow.title,
            ContentRow.url,
            _kst_day().label("day"),
            _label().label("topic"),
            ContentRow.scores["relevance"].as_float().label("relevance"),
            ContentRow.id.in_(published).label("published"),
        )
        .where(*filters)
        .order_by(ContentRow.collected_at.desc(), ContentRow.source)
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).all()), total


def archive_facets(session: Session) -> tuple[list, list]:
    """필터 칩에 쓸 주제·출처 목록과 각 건수."""
    label = _label()
    topics = list(
        session.execute(
            select(label.label("topic"), func.count().label("n")).group_by(label).order_by(func.count().desc())
        ).all()
    )
    sources = list(
        session.execute(
            select(ContentRow.source, func.count().label("n"))
            .group_by(ContentRow.source)
            .order_by(func.count().desc())
        ).all()
    )
    return topics, sources
