import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# docs/ARCHITECTURE.md §3.2 — 공통 Content 스키마 (Source Adapter가 공통으로 반환하는 형태)

SourceType = Literal["community", "code_repo", "paper", "blog"]
ContentStatus = Literal["collected", "filtered_out", "scored", "analyzed", "included", "excluded"]


class Content(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source: str
    source_type: SourceType
    author: Optional[str] = None
    title: str
    text: str
    url: str
    published_at: Optional[datetime] = None
    collected_at: datetime
    tags: list[str] = Field(default_factory=list)
    raw_data: dict = Field(default_factory=dict)

    content_hash: str = ""  # normalize 단계에서 계산됨
    engagement_metrics: dict = Field(default_factory=dict)
    language: Optional[str] = None

    scores: Optional[dict] = None
    llm_analysis: Optional[dict] = None
    status: ContentStatus = "collected"
