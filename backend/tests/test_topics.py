from types import SimpleNamespace

from app.pipeline import topic_label
from app.web.router import _page_window


class _Row:
    def __init__(self, title, scores=None):
        self.title = title
        self.scores = scores


class _Session:
    def __init__(self):
        self.commits = 0

    def add(self, _obj):
        pass

    def commit(self):
        self.commits += 1


def _response(items, input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        output_parsed=SimpleNamespace(items=[SimpleNamespace(n=n, topic=t) for n, t in items]),
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _patch(monkeypatch, responses):
    """responses를 순서대로 돌려주는 가짜 OpenAI 클라이언트를 끼운다."""
    calls = []

    class _Parse:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return responses[len(calls) - 1]

    class _Client:
        responses = _Parse()

    monkeypatch.setattr(topic_label, "OpenAI", lambda *a, **k: _Client())
    monkeypatch.setattr(topic_label.llm_client, "check_budget", lambda *a, **k: None)
    monkeypatch.setattr(topic_label.llm_client, "record_cost", lambda *a, **k: None)
    return calls


def test_labels_land_on_the_row_the_number_points_at(monkeypatch):
    rows = [_Row("a"), _Row("b"), _Row("c")]
    _patch(monkeypatch, [_response([(1, "RAG"), (3, "Agent")])])

    labelled, _ = topic_label.label_items(_Session(), rows, None)

    assert labelled == 2
    assert rows[0].scores["topic"] == "RAG"
    assert rows[2].scores["topic"] == "Agent"
    assert rows[1].scores is None  # 응답에 없던 번호는 라벨 없이 남는다


def test_out_of_range_numbers_are_dropped_rather_than_misapplied(monkeypatch):
    """번호가 어긋난 응답으로 엉뚱한 글에 라벨을 붙이면 대시보드가 거짓을 말한다."""
    rows = [_Row("a"), _Row("b")]
    _patch(monkeypatch, [_response([(0, "RAG"), (5, "Agent"), (2, "HCI")])])

    labelled, _ = topic_label.label_items(_Session(), rows, None)

    assert labelled == 1
    assert rows[0].scores is None
    assert rows[1].scores["topic"] == "HCI"


def test_unknown_category_names_fall_back_to_other(monkeypatch):
    rows = [_Row("a")]
    _patch(monkeypatch, [_response([(1, "Quantum Computing")])])

    topic_label.label_items(_Session(), rows, None)

    assert rows[0].scores["topic"] == topic_label.OTHER


def test_already_labelled_rows_are_not_sent_again(monkeypatch):
    rows = [_Row("a", {"topic": "RAG"}), _Row("b")]
    calls = _patch(monkeypatch, [_response([(1, "Agent")])])

    labelled, _ = topic_label.label_items(_Session(), rows, None)

    assert labelled == 1
    assert rows[1].scores["topic"] == "Agent"
    assert len(calls) == 1
    assert "a" not in calls[0]["input"][1]["content"]  # 이미 라벨 있는 글은 프롬프트에 안 들어간다


def test_nothing_to_label_makes_no_call(monkeypatch):
    rows = [_Row("a", {"topic": "RAG"})]
    calls = _patch(monkeypatch, [])

    assert topic_label.label_items(_Session(), rows, None) == (0, 0.0)
    assert calls == []


def test_every_title_is_sent_exactly_once_across_chunks(monkeypatch):
    rows = [_Row(f"title-{i}") for i in range(topic_label.CHUNK * 2 + 7)]
    calls = _patch(monkeypatch, [_response([]) for _ in range(3)])

    topic_label.label_items(_Session(), rows, None)

    assert len(calls) == 3
    sent = "\n".join(c["input"][1]["content"] for c in calls)
    for row in rows:
        assert sent.count(row.title + "\n") + sent.endswith(row.title) >= 1
    # 각 묶음은 1번부터 다시 세야 한다 — 전역 번호를 쓰면 두 번째 묶음이 전부 범위를 벗어난다
    assert all(c["input"][1]["content"].startswith("1. ") for c in calls)


def test_page_window_keeps_the_ends_and_the_neighbourhood():
    assert _page_window(1, 5) == [1, 2, 3, 4, 5]
    assert _page_window(6, 131) == [1, None, 4, 5, 6, 7, 8, None, 131]
    assert _page_window(1, 131) == [1, 2, 3, None, 131]
    assert _page_window(131, 131) == [1, None, 129, 130, 131]
