"""질의 레이어 — 수집한 글 전체에 자연어로 묻고 답을 받는다 (PRD §1 #9, docs/RAG_BLUEPRINT.md).

임베딩도 벡터 검색도 쓰지 않는다. 코퍼스 전체의 제목이 모델 컨텍스트에 들어가므로
모델이 직접 읽고 고르게 한다 — 단어가 겹치지 않는 개념 질의가 되고, 인덱스를
만들거나 임베딩 모델을 상주시킬 필요가 없다(= 대시보드 프로세스의 메모리가 늘지 않는다).

  1차  전체 목록을 N묶음으로 나눠 병렬로 후보 선정
  2차  선정분의 본문만 넣고 답변 생성

1차를 묶음으로 쪼개는 이유는 통제 실험 결과다(2026-10-01). 4,655건을 한 프롬프트에
넣으면 앞쪽만 뽑힌다 — 목록 순서만 섞어도 선정 결과의 겹침이 22%에 그쳤고, 날짜순일
때 9월 초에 몰렸던 선정이 섞으면 고르게 퍼졌다. 묶음을 짧게 하면 '흘릴 가운데'가 없다.
총 토큰과 비용은 거의 같고 호출 수만 늘어난다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.llm import client as llm_client

# repository.get_corpus_for_ask()가 돌려주는 행 — ORM 객체가 아니라 필요한 컬럼만 담은
# 가벼운 Row다(id/source/title/url/collected_at/text). 속성 이름만 맞으면 된다.

MODEL = "gpt-6-luna"
N_GROUPS = 10
PER_GROUP = 10        # 상한 5로 돌리면 모든 묶음이 상한에 닿아 결과가 잘렸다(2026-10-01)
BODY_CHARS = 2500
TITLE_CHARS = 200

def _select_system(today: date, span: str, full_span: str) -> str:
    """묶음마다 자기 구간만 보이므로 바깥 맥락을 명시해 준다.

    이게 없으면 "지난주" 같은 기간 질문에서 9월 초 묶음이 자기가 지난주인 줄 알고
    에이전트 글을 가득 채운다 — 배포 후 첫 질의에서 실제로 그랬다(2026-10-02).
    전체 범위도 같이 줘야 이 묶음이 어디쯤인지 알 수 있다."""
    return (
        "너는 개인용 기술 뉴스 아카이브의 사서다. 사용자의 질문에 답하는 데 실제로 "
        "도움이 될 글의 번호만 골라라.\n"
        f"- 오늘은 {today.isoformat()}이다\n"
        f"- 아카이브 전체 범위는 {full_span}이고, **아래 목록은 그중 {span} 구간만**이다\n"
        f"- 이 묶음에서 최대 {PER_GROUP}건까지\n"
        "- **질문이 기간을 한정하는데(예: 지난주, 이번 달) 이 구간이 거기에 안 들어가면 "
        "빈 목록을 돌려라.** 내용이 비슷해 보여도 기간 밖이면 고르지 마라\n"
        "- 기간과 무관하게 이 묶음에 관련 글이 없으면 그때도 빈 목록을 돌려라\n"
        "- 제목만 보고 판단해야 하니, 애매하면 넣는 쪽으로\n"
        "- 질문이 개념적이면 단어가 겹치지 않아도 내용이 맞을 것 같은 글을 골라라"
    )

_ANSWER_SYSTEM = (
    "너는 개인용 기술 뉴스 아카이브의 사서다. 아래 글들만 근거로 질문에 한국어로 답해라.\n"
    "- 결론 먼저, 그다음 근거. 근거에는 [번호]를 달아라\n"
    "- 아카이브에 없는 내용은 지어내지 말고, 모자라면 모자라다고 적어라\n"
    "- 본문 없이 제목만 있는 글은 '제목만 있어 확인 못 함'이라고 구분해라\n"
    "- 벤더가 자기 제품을 설명하는 글과 제3자 평가를 구분해서 읽어라"
)


class _Picks(BaseModel):
    numbers: list[int] = Field(description=f"관련된 글 번호, 이 묶음에서 최대 {PER_GROUP}개")


class _Answer(BaseModel):
    answer: str = Field(description="한국어 답변. 근거가 된 글은 [번호]로 표시")
    used: list[int] = Field(description="실제로 답에 쓴 글 번호")
    confidence: str = Field(description="높음/보통/낮음 — 주어진 글만으로 답하기에 충분했는지")


@dataclass
class AskResult:
    question: str
    answer: str
    confidence: str
    picked: list       # 1차가 고른 글 (답에 다 쓰이지는 않는다)
    used: list         # 답변이 실제로 인용한 글
    cost_usd: float
    calls: int


def _chunks(rows: list) -> list[list[int]]:
    size = -(-len(rows) // N_GROUPS)
    return [list(range(i, min(i + size, len(rows)))) for i in range(0, len(rows), size)]


def ask(session: Session, rows: list, question: str, run_date: date) -> AskResult:
    """rows(코퍼스 전체)를 놓고 question에 답한다. 비용은 질의 전용 예산으로 따로 센다."""
    llm_client.require_api_key()
    llm_client.check_ask_budget(session, run_date)
    api = OpenAI()
    total_cost, calls = 0.0, 0

    def line(i: int) -> str:
        r = rows[i]
        day = r.collected_at.date().isoformat() if r.collected_at else "?"
        return f"{i}\t{day}\t{r.source}\t{(r.title or '')[:TITLE_CHARS]}"

    def day_of(i: int) -> str:
        return rows[i].collected_at.date().isoformat() if rows[i].collected_at else "?"

    full_span = f"{day_of(0)}~{day_of(len(rows) - 1)}" if rows else "-"

    def select(idxs: list[int]) -> tuple[list[int], int, int]:
        catalog = "\n".join(line(i) for i in idxs)
        span = f"{day_of(idxs[0])}~{day_of(idxs[-1])}"
        res = api.responses.parse(
            model=MODEL,
            input=[{"role": "system", "content": _select_system(run_date, span, full_span)},
                   {"role": "user", "content": f"질문: {question}\n\n목록:\n{catalog}"}],
            text_format=_Picks,
        )
        allowed = set(idxs)
        picked = [n for n in res.output_parsed.numbers if n in allowed][:PER_GROUP]
        return picked, res.usage.input_tokens, res.usage.output_tokens

    groups = _chunks(rows)
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        results = list(pool.map(select, groups))

    picked_idx: list[int] = []
    for got, in_tok, out_tok in results:
        picked_idx += got
        calls += 1
        llm_client.record_cost(session, run_date, "ask_select", MODEL, in_tok, out_tok)
        total_cost += float(llm_client._cost_usd(MODEL, in_tok, out_tok))

    if not picked_idx:
        return AskResult(question, "아카이브에서 질문과 관련된 글을 찾지 못했습니다.",
                         "낮음", [], [], total_cost, calls)

    docs = []
    for i in picked_idx:
        r = rows[i]
        day = r.collected_at.date().isoformat() if r.collected_at else "?"
        body = (r.text or "")[:BODY_CHARS] or "(본문 없음 — 제목만)"
        docs.append(f"[{i}] {day} | {r.source} | {r.title}\nURL: {r.url}\n{body}")

    res = api.responses.parse(
        model=MODEL,
        input=[{"role": "system", "content": _ANSWER_SYSTEM},
               {"role": "user", "content": f"질문: {question}\n\n자료:\n" + "\n---\n".join(docs)}],
        text_format=_Answer,
    )
    calls += 1
    llm_client.record_cost(session, run_date, "ask_answer", MODEL,
                           res.usage.input_tokens, res.usage.output_tokens)
    total_cost += float(llm_client._cost_usd(MODEL, res.usage.input_tokens, res.usage.output_tokens))

    out = res.output_parsed
    used_idx = [i for i in out.used if 0 <= i < len(rows)]
    return AskResult(
        question=question,
        answer=out.answer,
        confidence=out.confidence,
        picked=[rows[i] for i in picked_idx],
        used=[rows[i] for i in used_idx],
        cost_usd=total_cost,
        calls=calls,
    )
