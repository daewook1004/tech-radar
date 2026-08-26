import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Content(Base):
    """docs/ARCHITECTURE.md §4.1 — 수집된 원문 (90일 롤링 보존)."""

    __tablename__ = "content"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    author: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    content_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    engagement_metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    language: Mapped[str | None] = mapped_column(String)

    scores: Mapped[dict | None] = mapped_column(JSONB)
    llm_analysis: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, nullable=False, default="collected")


class PipelineRun(Base):
    """docs/ARCHITECTURE.md §4.2 — 실행 이력 (실패 처리·관찰성)."""

    __tablename__ = "pipeline_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)  # running|success|partial_failure|failed
    failures: Mapped[list] = mapped_column(JSONB, default=list)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)


class Digest(Base):
    """docs/ARCHITECTURE.md §4.3 — 그날의 Tech Radar."""

    __tablename__ = "digest"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(
        Date, ForeignKey("pipeline_run.run_date"), nullable=False, unique=True
    )
    generated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    trend_summary: Mapped[str | None] = mapped_column(Text)
    email_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    email_status: Mapped[str | None] = mapped_column(String)

    items: Mapped[list["DigestItem"]] = relationship(back_populates="digest")


class DigestItem(Base):
    __tablename__ = "digest_item"
    __table_args__ = (UniqueConstraint("digest_id", "content_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    digest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("digest.id"), nullable=False)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    final_score: Mapped[float] = mapped_column(Numeric, nullable=False)
    is_diversity_pick: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    digest: Mapped["Digest"] = relationship(back_populates="items")


class CostLedger(Base):
    """docs/ARCHITECTURE.md §4.4 — LLM 비용 추적 (예산 상한 안전장치)."""

    __tablename__ = "cost_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
