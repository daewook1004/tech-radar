from datetime import date

from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Content as ContentRow
from app.llm import client as llm_client

MODEL = "gpt-5.6-luna"  # 공식 문서상 "cost-sensitive, high-volume workloads" 전용 모델

SYSTEM_PROMPT = (
    "너는 AI/개발 기술 뉴스를 평가하는 애널리스트다. 주어진 글 하나를 읽고 "
    "Importance(중요도), Novelty(새로움), Credibility(신뢰도)를 각각 1~10점으로 채점하고 "
    "한 줄 평가 이유를 한국어로 작성해라."
)


class ScoreOutput(BaseModel):
    importance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    credibility: int = Field(ge=1, le=10)
    reason: str


def score_items(session: Session, rows: list[ContentRow], run_date: date) -> list[ContentRow]:
    """PRD §5.4 1차(gpt-5.6-luna) — 임베딩 필터를 통과한 후보 전체에 대해 스코어링."""
    client = OpenAI()
    for row in rows:
        llm_client.check_budget(session, run_date)

        user_content = f"제목: {row.title}\n출처: {row.source}\n본문 발췌: {(row.text or '')[:1500]}"
        response = client.responses.parse(
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            text_format=ScoreOutput,
        )
        result = response.output_parsed

        row.scores = {
            **(row.scores or {}),
            "importance": result.importance,
            "novelty": result.novelty,
            "credibility": result.credibility,
            "score_reason": result.reason,
        }
        row.status = "scored"
        llm_client.record_cost(
            session, run_date, "luna_scoring", MODEL, response.usage.input_tokens, response.usage.output_tokens
        )
    session.commit()
    return rows
