from datetime import date

from openai import OpenAI
from sqlalchemy.orm import Session

from app.db.models import Content as ContentRow
from app.llm import client as llm_client

MODEL = "gpt-5.6-sol"

SYSTEM_PROMPT = (
    "너는 오늘 수집된 AI/개발 기술 뉴스 후보군 전체를 훑어보고, "
    "반복적으로 등장하는 주제나 흐름을 3~5줄로 한국어로 요약하는 애널리스트다. "
    "개별 글이 아니라 전체적인 트렌드/패턴에 집중해라."
)


def summarize_trends(session: Session, rows: list[ContentRow], run_date: date) -> str:
    """PRD §5.4 — 1차 필터 통과 후보군 전체 대상 "오늘의 주요 흐름" 1회 호출."""
    if not rows:
        return ""

    llm_client.check_budget(session, run_date)
    client = OpenAI()

    listing = "\n".join(f"- {r.title} (태그: {', '.join(r.tags or [])})" for r in rows)
    response = client.responses.create(
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"오늘의 후보 목록:\n{listing}"},
        ],
    )
    text = response.output_text

    llm_client.record_cost(
        session, run_date, "trend_summary", MODEL, response.usage.input_tokens, response.usage.output_tokens
    )
    return text
