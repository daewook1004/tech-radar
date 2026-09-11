import uuid

from app.db.models import Content as ContentRow
from app.pipeline.fetch_body import MIN_BODY_CHARS, extract_body, has_body
from app.pipeline.rank import _rank_within, rrf_scores

PARA = "Agents are moving off local machines and onto hosted sandboxes that keep running after you close the lid. "


def _row(text: str = "", **scores) -> ContentRow:
    return ContentRow(id=uuid.uuid4(), title="t", text=text, scores=scores)


def test_extract_body_keeps_paragraphs_and_drops_page_chrome():
    html = f"""<html><head><script>var tracking = 1;</script></head><body>
      <nav><p>Home About Blog Careers and every other menu item on this site</p></nav>
      <article><p>{PARA}</p><p>Share</p><p>{PARA}</p></article>
      <footer><p>Copyright 2026 Example Inc. All rights reserved worldwide.</p></footer>
    </body></html>"""
    body = extract_body(html)
    assert body.count("Agents are moving") == 2
    assert "Home About" not in body and "Copyright" not in body and "tracking" not in body
    assert "Share" not in body  # 짧은 조각은 버린다


def test_extract_body_prefers_citation_abstract():
    abstract = "We show that language models can declare which parts of the context they need. " * 4
    html = f'<meta name="citation_abstract" content="{abstract}"><p>arXivLabs is a framework that allows collaborators.</p>'
    assert extract_body(html).startswith("We show that language models")


def test_extract_body_handles_unclosed_paragraphs():
    html = f"<p>{PARA}<p>{PARA}"
    assert extract_body(html).count("Agents are moving") == 2


def test_extract_body_reads_bullet_summary_and_skips_comments_outside_it():
    # GeekNews 토픽 페이지: 요약은 section#topic_contents의 글머리표, 댓글은 그 밖의 <p>
    html = f"""<div class="topictitle">Title</div>
      <section id="topic_contents"><ul><li>{PARA}</li><li>{PARA}</li></ul></section>
      <div class="comment"><p>2025년에 KISA가 무슨 근거로 인증을 갱신한 건지 의문입니다. 이 조치 여부도 확인해야 합니다.</p></div>"""
    body = extract_body(html)
    assert body.count("Agents are moving") == 2
    assert "KISA" not in body


def test_extract_body_ignores_lists_outside_the_content_area():
    html = f"""<div class="menu"><ul><li>Pricing plans for teams and enterprises of every size</li></ul></div>
      <p>There was an error while loading. Please reload this page.</p>
      <p>{PARA}</p><p>{PARA}</p>"""
    body = extract_body(html)
    assert "Pricing" not in body and "error while loading" not in body
    assert body.count("Agents are moving") == 2


def test_extract_body_gives_up_on_script_only_pages():
    html = "<html><body><div id='root'></div><p>Kakao brings tomorrow's technology into your life</p></body></html>"
    assert extract_body(html) == ""


def test_has_body_threshold():
    assert not has_body(_row("x" * (MIN_BODY_CHARS - 1)))
    assert has_body(_row("x" * MIN_BODY_CHARS))


def test_unscored_rows_get_the_middle_rank_not_last():
    rows = [_row(importance=9), _row(importance=5), _row(importance=1), _row()]
    ranks = _rank_within(rows, "importance")
    assert [ranks[r.id] for r in rows[:3]] == [1, 2, 3]
    assert ranks[rows[3].id] == 2  # 점수 있는 3건의 가운데


def test_unscored_row_lands_between_high_and_low_llm_scores_at_equal_relevance():
    # relevance는 셋 다 비워 완전히 동률로 둔다 — 같은 값을 넣으면 입력 순서로 동점이 갈려 비교가 흐려진다
    high = _row(importance=9, novelty=9, credibility=9)
    low = _row(importance=1, novelty=1, credibility=1)
    unknown = _row()
    rrf = rrf_scores([high, low, unknown])
    assert rrf[high.id] > rrf[unknown.id] > rrf[low.id]
