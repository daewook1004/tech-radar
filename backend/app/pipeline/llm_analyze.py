from datetime import date

from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_interests
from app.db.models import Content as ContentRow
from app.llm import client as llm_client

MODEL = "gpt-5.6-sol"  # 최상위 플래그십 — 하루 5~10건뿐이라 비용보다 품질 우선(사용자 확정)


def _system_prompt() -> str:
    """topic 예시는 interests.yaml에서 직접 읽어온다 — 예전엔 프롬프트에 카테고리 이름을
    하드코딩해뒀는데, 관심사를 추가해도 프롬프트가 그대로라 LLM이 새 카테고리 이름을
    모르는 채로 topic을 붙였다. rank.py의 다양성 보장은 'LLM이 붙인 topic == high 카테고리
    이름'으로 대조하므로, 둘이 어긋나면 다양성 보장이 조용히 작동을 멈춘다(2026-09-07)."""
    names = [cat["name"] for cats in get_interests().values() for cat in cats]
    return (
        "너는 AI/개발 기술 뉴스를 깊이 있게 분석하는 애널리스트다. 주어진 글을 읽고 "
        "핵심 요약, 왜 중요한지에 대한 설명, 주제(topic), 핵심 키워드, 콘텐츠 유형을 "
        "모두 한국어로 작성해라. topic은 아래 관심사 카테고리 이름 중 가장 가까운 것을 "
        f"그대로 사용해라 (해당하는 게 없을 때만 새로 만들어라): {', '.join(names)}."
    )


class AnalysisOutput(BaseModel):
    summary: str
    why_important: str
    topic: str
    keywords: list[str]
    content_type: str


def analyze_items(session: Session, rows: list[ContentRow], run_date: date) -> list[ContentRow]:
    """PRD §5.4 2차(gpt-5.6-sol) — 상위 후보만 정밀 요약 + 추천 이유."""
    client = OpenAI()
    for row in rows:
        llm_client.check_budget(session, run_date)

        relevance = (row.scores or {}).get("relevance")
        user_content = (
            f"제목: {row.title}\n출처: {row.source}\n"
            f"본문: {(row.text or '')[:4000]}\n"
            f"참고 - 관심사 관련도(0~1): {relevance}"
        )
        response = client.responses.parse(
            model=MODEL,
            input=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user_content},
            ],
            text_format=AnalysisOutput,
        )
        result = response.output_parsed

        row.llm_analysis = {
            "summary": result.summary,
            "why_important": result.why_important,
            "topic": result.topic,
            "keywords": result.keywords,
            "content_type": result.content_type,
        }
        row.status = "analyzed"
        llm_client.record_cost(
            session, run_date, "sol_analysis", MODEL, response.usage.input_tokens, response.usage.output_tokens
        )
    session.commit()
    return rows
