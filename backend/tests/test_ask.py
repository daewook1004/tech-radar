from datetime import date
from types import SimpleNamespace

import pytest

from app.llm import ask as ask_module
from app.llm.client import BudgetExceededError, check_ask_budget

RUN_DATE = date(2026, 10, 2)


class _Session:
    """scalar()만 쓰는 가짜 세션 — 예산 가드가 합계를 어떻게 읽는지만 본다."""

    def __init__(self, spent):
        self.spent = spent

    def scalar(self, _stmt):
        return self.spent


def test_ask_budget_blocks_when_todays_queries_hit_the_limit():
    with pytest.raises(BudgetExceededError):
        check_ask_budget(_Session(0.5), RUN_DATE)


def test_ask_budget_allows_under_the_limit():
    assert check_ask_budget(_Session(0.12), RUN_DATE) is None


def test_every_row_lands_in_exactly_one_group():
    # 묶음이 겹치거나 빠지면 어떤 글은 두 번 평가되고 어떤 글은 아예 안 보인다
    for n in (1, 9, 10, 11, 4655):
        rows = list(range(n))
        groups = ask_module._chunks(rows)
        flat = [i for g in groups for i in g]
        assert sorted(flat) == list(range(n))
        assert len(groups) <= ask_module.N_GROUPS


def test_groups_are_evenly_sized():
    # 한 묶음만 길면 그 묶음에서 다시 '가운데를 흘리는' 문제가 생긴다
    groups = ask_module._chunks(list(range(4655)))
    sizes = [len(g) for g in groups]
    assert max(sizes) - min(sizes) <= max(sizes) * 0.5


def test_pipeline_budget_is_not_charged_for_queries():
    # 질의 비용은 ask_ 접두어로 기록돼 파이프라인 예산과 섞이지 않아야 한다.
    # 섞이면 질문을 많이 한 날 다음 날 아침 다이제스트가 거부된다.
    assert ask_module.ask.__module__ == "app.llm.ask"
    import inspect

    src = inspect.getsource(ask_module)
    assert '"ask_select"' in src and '"ask_answer"' in src
    assert src.count("check_ask_budget") == 1
    assert "check_budget(" not in src


def test_rows_without_a_body_are_marked_for_the_model():
    # 본문이 없는 글을 그냥 빈칸으로 넘기면 모델이 '내용이 없는 글'인지 모른다
    import inspect

    src = inspect.getsource(ask_module.ask)
    assert "(본문 없음 — 제목만)" in src


def test_result_is_empty_when_nothing_was_picked():
    captured = {}

    def fake_record(session, run_date, stage, model, i, o):
        captured.setdefault(stage, 0)
        captured[stage] += 1

    class _Api:
        class responses:
            @staticmethod
            def parse(**kwargs):
                return SimpleNamespace(
                    output_parsed=SimpleNamespace(numbers=[]),
                    usage=SimpleNamespace(input_tokens=10, output_tokens=1),
                )

    rows = [SimpleNamespace(id=i, source="s", title=f"t{i}", url="u",
                            collected_at=None, text="") for i in range(20)]
    import app.llm.client as llm_client

    orig_record, orig_guard, orig_key, orig_openai = (
        llm_client.record_cost, llm_client.check_ask_budget,
        llm_client.require_api_key, ask_module.OpenAI,
    )
    llm_client.record_cost = fake_record
    llm_client.check_ask_budget = lambda *a, **k: None
    llm_client.require_api_key = lambda: None
    ask_module.OpenAI = lambda: _Api()
    try:
        result = ask_module.ask(None, rows, "아무거나", RUN_DATE)
    finally:
        (llm_client.record_cost, llm_client.check_ask_budget,
         llm_client.require_api_key, ask_module.OpenAI) = (
            orig_record, orig_guard, orig_key, orig_openai)

    # 아무것도 못 골랐으면 2차를 부르지 않는다 (쓸데없는 호출과 비용 방지)
    assert result.picked == [] and result.used == []
    assert result.confidence == "낮음"
    assert "ask_answer" not in captured
