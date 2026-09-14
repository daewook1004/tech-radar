from app.collectors.arxiv import clean_abstract, interleave, is_wanted

KEYWORDS = ["agent", "evaluation"]


def _entry(announce_type: str = "new", title: str = "On agent teams", summary: str = "") -> dict:
    return {"arxiv_announce_type": announce_type, "title": title, "summary": summary}


def test_clean_abstract_strips_the_announce_prefix():
    summary = "arXiv:2609.11934v1 Announce Type: new  Abstract: In networked dynamical systems, ..."
    assert clean_abstract(summary) == "In networked dynamical systems, ..."


def test_clean_abstract_keeps_text_without_the_prefix():
    assert clean_abstract("  그냥 초록  ") == "그냥 초록"


def test_is_wanted_skips_revisions_of_old_papers():
    # replace는 예전 논문의 개정판이라 '오늘의 소식'이 아니다
    assert is_wanted(_entry("new"), KEYWORDS)
    assert is_wanted(_entry("cross"), KEYWORDS)
    assert not is_wanted(_entry("replace"), KEYWORDS)
    assert not is_wanted(_entry("replace-cross"), KEYWORDS)


def test_is_wanted_needs_a_keyword_somewhere():
    assert not is_wanted(_entry(title="On protein folding"), KEYWORDS)
    assert is_wanted(_entry(title="On protein folding", summary="We run an evaluation of ..."), KEYWORDS)


def test_interleave_takes_turns_between_categories():
    # 카테고리 피드는 하루 수백 건이라, 앞 카테고리가 상한을 다 먹으면 뒤는 아예 못 들어온다
    assert interleave([["a1", "a2", "a3"], ["b1"], ["c1", "c2"]], 10) == ["a1", "b1", "c1", "a2", "c2", "a3"]
    assert interleave([["a1", "a2"], ["b1", "b2"]], 3) == ["a1", "b1", "a2"]
