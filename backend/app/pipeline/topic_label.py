"""수집분 전체에 주제 라벨을 붙인다 — /topics 대시보드의 가로축.

왜 LLM인가: embed_filter가 이미 16개 카테고리와의 유사도를 계산하므로 argmax를 공짜로
쓸 수 있고 처음에 그렇게 했다. 그런데 LLM이 주제를 매긴 809건과 비교해 보니 일치율이
35.1%였다(임계치를 cos 0.55까지 올려도 42.1%). 틀리는 방식도 체계적이어서
LLM Evaluation 91건을 임베딩은 Inference(28건)로, AI Coding 73건을 AI Product(18건)로
보냈다 — 카테고리 설명의 표면 어휘에 끌려간다. Agent만 56%로 맞았다. 그 라벨로 추이를
그리면 없는 추세를 보여주게 되므로 쓰지 않는다. 측정 기록은 docs/eval/topic-labels/.

제목만 보내서 하루 300건에 약 $0.0014(월 $0.04)다. 본문을 넣지 않는 이유는 둘이다:
수집분 전체의 69%만 본문이 있어서 본문을 쓰면 라벨 품질이 소스마다 달라지고,
주제는 제목이면 거의 정해진다(본문은 "얼마나 중요한가"에 필요한 것이고 그건 llm_score의 일이다).
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_interests
from app.db.models import Content as ContentRow
from app.llm import client as llm_client

logger = logging.getLogger(__name__)

MODEL = "gpt-6-luna"
STAGE = "topic_label"

# 한 번에 보내는 제목 수. 300건을 한 프롬프트에 넣으면 출력이 길어져 번호가 어긋날 위험이
# 커지고, 너무 작게 쪼개면 호출 수만 늘어난다. 50이면 호출 6번으로 끝난다.
CHUNK = 50
TITLE_CHARS = 200

# 어느 카테고리에도 안 맞으면 이 라벨을 쓴다 — 억지로 끼워 맞춘 주제가 가장 나쁘다.
OTHER = "기타"


class _Labelled(BaseModel):
    n: int
    topic: str


class _LabelBatch(BaseModel):
    items: list[_Labelled]


def _system_prompt() -> str:
    """관심사 카테고리를 그대로 선택지로 준다 — 대시보드의 세로축이 설정 파일과 같아야 한다."""
    lines = []
    for tier, categories in get_interests().items():
        for cat in categories:
            lines.append(f"- {cat['name']}: {cat['description']}")
    return (
        "너는 AI/개발 기술 글을 주제별로 분류한다. 아래 분류 중에서 각 글에 가장 잘 맞는 "
        "것 하나를 고르고, 어디에도 맞지 않으면 "
        f'"{OTHER}"를 써라. 분류 이름은 아래 표기 그대로 적어라.\n\n'
        + "\n".join(lines)
        + "\n\n제목만 주어진다. 추측이 어려우면 억지로 맞추지 말고 "
        f'"{OTHER}"를 골라라. 입력의 번호를 그대로 돌려줘야 한다.'
    )


def _chunk_input(rows: list[ContentRow]) -> str:
    return "\n".join(f"{i}. {row.title[:TITLE_CHARS]}" for i, row in enumerate(rows, start=1))


def label_items(session: Session, rows: list[ContentRow], run_date: date) -> tuple[int, float]:
    """rows에 주제 라벨을 붙여 scores['topic']에 저장한다. 반환값은 (라벨 붙인 수, 비용 USD).

    실패해도 다이제스트에는 영향이 없어야 한다 — 이 단계는 발송이 끝난 뒤에 돌고,
    호출이 깨지면 그 묶음만 라벨 없이 남는다(대시보드에서 '기타'로 묶인다).
    """
    targets = [r for r in rows if not (r.scores or {}).get("topic")]
    if not targets:
        return 0, 0.0

    valid = {cat["name"] for categories in get_interests().values() for cat in categories}
    valid.add(OTHER)
    system = _system_prompt()
    client = OpenAI()
    chunks = [targets[i : i + CHUNK] for i in range(0, len(targets), CHUNK)]

    llm_client.check_budget(session, run_date)

    def run(chunk: list[ContentRow]):
        response = client.responses.parse(
            model=MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": _chunk_input(chunk)},
            ],
            text_format=_LabelBatch,
        )
        return chunk, response

    labelled = 0
    cost = 0.0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for chunk, response in ex.map(run, chunks):
            llm_client.record_cost(
                session, run_date, STAGE, MODEL, response.usage.input_tokens, response.usage.output_tokens
            )
            cost += float(
                llm_client._cost_usd(MODEL, response.usage.input_tokens, response.usage.output_tokens)
            )
            for item in (response.output_parsed.items if response.output_parsed else []):
                if not 1 <= item.n <= len(chunk):
                    continue  # 번호가 어긋난 응답은 버린다 — 엉뚱한 글에 라벨을 붙이는 게 더 나쁘다
                row = chunk[item.n - 1]
                topic = item.topic.strip()
                row.scores = {**(row.scores or {}), "topic": topic if topic in valid else OTHER}
                labelled += 1

    session.commit()
    logger.info("labelled %d/%d items with a topic (%d calls, $%.4f)", labelled, len(targets), len(chunks), cost)
    return labelled, cost


def main() -> None:
    """과거분 백필 — `python -m app.pipeline.topic_label`.

    라벨이 없는 행만 골라서 돌리므로 중간에 끊겨도 다시 실행하면 이어서 한다.
    """
    import sys

    from app.db.session import get_session

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    session = get_session()
    rows = [
        row
        for row in session.query(ContentRow).order_by(ContentRow.collected_at.desc()).all()
        if not (row.scores or {}).get("topic")
    ]
    print(f"라벨 없는 글 {len(rows)}건", flush=True)
    total_cost = 0.0
    batch = 500
    for start in range(0, len(rows), batch):
        n, cost = label_items(session, rows[start : start + batch], date.today())
        total_cost += cost
        print(f"  {start + len(rows[start:start + batch])}/{len(rows)} — 누적 ${total_cost:.4f}", flush=True)
    print(f"끝. 라벨 {len(rows)}건, 비용 ${total_cost:.4f}")


if __name__ == "__main__":
    main()
