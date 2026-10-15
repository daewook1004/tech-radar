import re
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown as _markdown
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.db import repository
from app.db.session import get_db
from app.llm import ask as ask_module
from app.llm.client import BudgetExceededError, MissingAPIKeyError
from app.web.auth import require_basic_auth

router = APIRouter(dependencies=[Depends(require_basic_auth)])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_MD_MARKER_RE = re.compile(r"^[\-\*]\s+|\*\*|__|`")


def _render_markdown(text: str | None) -> Markup:
    """LLM이 내보내는 마크다운(굵게/목록)을 이메일(plain text)과 달리 웹에서는 실제로 렌더링한다."""
    if not text:
        return Markup("")
    html = _markdown.markdown(text, extensions=["nl2br"])
    return Markup(html)


def _strip_markdown(text: str | None) -> str:
    """목록 페이지 미리보기용 — 잘린 텍스트를 HTML로 렌더링하면 태그가 깨지므로,
    마크다운 기호만 제거한 순수 텍스트로 보여준다."""
    if not text:
        return ""
    lines = [_MD_MARKER_RE.sub("", line.strip()) for line in text.splitlines()]
    return " ".join(line for line in lines if line)


def _cite_links(text: str, cited: list) -> str:
    """답변 속 [1][2]를 원문으로 가는 링크로 바꾼다. 번호만 떠 있으면 클릭할 데가 없다."""
    if not text:
        return ""

    def swap(m):
        n = int(m.group(1))
        if not 1 <= n <= len(cited):
            return m.group(0)
        row = cited[n - 1]
        title = escape(row.title or "")
        return (f'<a href="{escape(row.url or "#")}" target="_blank" rel="noopener" '
                f'title="{title}" class="cite">[{n}]</a>')

    return re.sub(r"\[(\d+)\]", swap, text)


def _now_kst() -> date:
    """파이프라인과 같은 날짜 기준 — cost_ledger가 KST 달력 날짜로 묶여 있다."""
    from datetime import datetime

    return datetime.now(ZoneInfo("Asia/Seoul")).date()


_templates.env.filters["cite_links"] = _cite_links
_templates.env.filters["markdown"] = _render_markdown
_templates.env.filters["strip_markdown"] = _strip_markdown


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/digests")


@router.get("/digests")
def digest_list(request: Request, session: Session = Depends(get_db)):
    digests = repository.list_digests(session)
    return _templates.TemplateResponse(
        request, "digest_list.html", {"digests": digests, "nav": "digests"}
    )


@router.get("/digests/{run_date}")
def digest_detail(request: Request, run_date: date, session: Session = Depends(get_db)):
    result = repository.get_digest_detail(session, run_date)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{run_date.isoformat()} 다이제스트가 없습니다")
    digest, items = result

    all_content = repository.get_content_by_date(session, run_date)
    selected_ids = {content.id for _, content in items}
    by_source: dict[str, list] = {}
    for row in all_content:
        by_source.setdefault(row.source, []).append(row)
    for rows in by_source.values():
        rows.sort(key=lambda r: (r.scores or {}).get("relevance", 0), reverse=True)

    return _templates.TemplateResponse(
        request,
        "digest_detail.html",
        {
            "nav": "digests",
            "digest": digest,
            "items": items,
            "by_source": dict(sorted(by_source.items())),
            "selected_ids": selected_ids,
        },
    )


@router.get("/ask")
def ask(request: Request, q: str = "", session: Session = Depends(get_db)):
    """수집한 글 전체에 자연어로 묻는다 — 다이제스트는 관심사 필터를 통과한 하루 10건만
    보여주지만, 여기서는 걸러진 것까지 전부가 대상이다 (docs/RAG_BLUEPRINT.md)."""
    question = q.strip()
    if not question:
        return _templates.TemplateResponse(request, "ask.html", {"question": "", "result": None, "nav": "ask"})

    # run_date는 파이프라인과 같은 기준(KST 달력 날짜)으로 — 비용 원장이 날짜별로 묶인다
    rows = repository.get_corpus_for_ask(session, ask_module.BODY_CHARS)
    try:
        result = ask_module.ask(session, rows, question, _now_kst())
    except (BudgetExceededError, MissingAPIKeyError) as exc:
        return _templates.TemplateResponse(
            request, "ask.html", {"question": question, "result": None, "error": str(exc), "nav": "ask"}
        )
    return _templates.TemplateResponse(
        request, "ask.html", {"question": question, "result": result, "corpus_size": len(rows), "nav": "ask"}
    )


ARCHIVE_PER_PAGE = 50
_HEATMAP_WEEKS = 8
_TREND_DAYS = 7


def _page_window(page: int, pages: int) -> list:
    """1 … 4 5 [6] 7 8 … 131 — 끝과 현재 주변만 보여준다. None은 생략 표시."""
    if pages <= 7:
        return list(range(1, pages + 1))
    nearby = {1, pages, page, page - 1, page + 1, page - 2, page + 2}
    shown = sorted(n for n in nearby if 1 <= n <= pages)
    out: list = []
    for n in shown:
        if out and n - out[-1] > 1:
            out.append(None)
        out.append(n)
    return out


@router.get("/topics")
def topics(request: Request, session: Session = Depends(get_db)):
    """내용 쪽 대시보드 — 어떤 주제가 얼마나 나왔나.

    라벨은 topic_label.py가 제목만 보고 매긴 것(scores['topic'])이다 — 라벨이 없는 글과
    어느 분류에도 안 맞는 글은 '기타'로 묶인다. 다이제스트에 실린 10건이 아니라
    수집분 전체가 대상이다 — '내가 받은 것'이 아니라 '들어온 것'을 보는 화면이다."""
    rows = repository.topic_weeks(session, weeks=_HEATMAP_WEEKS)
    weeks = sorted({r.week for r in rows})
    totals: dict[str, int] = {}
    cells: dict[tuple[str, object], int] = {}
    for r in rows:
        totals[r.topic] = totals.get(r.topic, 0) + r.n
        cells[(r.topic, r.week)] = r.n
    order = sorted(totals, key=lambda t: (t == repository.OTHER_LABEL, -totals[t]))
    peak = max(cells.values(), default=0)

    heatmap = [
        {
            "topic": topic,
            "total": totals[topic],
            "cells": [
                {"week": w, "n": cells.get((topic, w), 0),
                 "intensity": round(cells.get((topic, w), 0) / peak, 3) if peak else 0}
                for w in weeks
            ],
        }
        for topic in order
    ]

    trend = repository.topic_trend(session, days=_TREND_DAYS)
    moves = sorted(
        ({"topic": t.topic, "recent": t.recent, "previous": t.previous, "delta": t.recent - t.previous}
         for t in trend if t.topic != repository.OTHER_LABEL),
        key=lambda m: -abs(m["delta"]),
    )[:8]
    widest = max((abs(m["delta"]) for m in moves), default=0)
    for m in moves:
        m["width"] = round(100 * abs(m["delta"]) / widest, 1) if widest else 0

    blind = repository.topic_blindspots(session, days=14)
    days = repository.collection_days(session, days=14)
    busiest = max((d.collected for d in days), default=0)

    this_week = weeks[-1] if weeks else None
    return _templates.TemplateResponse(
        request,
        "topics.html",
        {
            "nav": "topics",
            "weeks": weeks,
            "heatmap": heatmap,
            "moves": moves,
            "blind": [b for b in blind if b.topic != repository.OTHER_LABEL],
            "days": [{"day": d.day, "collected": d.collected, "published": d.published,
                      "height": round(100 * d.collected / busiest, 1) if busiest else 0}
                     for d in days],
            "week_collected": sum(c["n"] for row in heatmap for c in row["cells"] if c["week"] == this_week),
            "window_collected": sum(d.collected for d in days),
            "window_published": sum(d.published for d in days),
            "top_topic": order[0] if order and order[0] != repository.OTHER_LABEL else (order[1] if len(order) > 1 else None),
            "riser": next((m for m in moves if m["delta"] > 0), None),
            "trend_days": _TREND_DAYS,
        },
    )


@router.get("/archive")
def archive(
    request: Request,
    topic: str = "",
    source: str = "",
    q: str = "",
    page: int = 1,
    session: Session = Depends(get_db),
):
    """수집한 글 전체를 그냥 넘겨보는 화면 — /ask는 질문당 2센트 30초라 '쭉 훑어보기'에는 못 쓴다."""
    page = max(1, page)
    rows, total = repository.archive_page(
        session,
        topic=topic.strip() or None,
        source=source.strip() or None,
        q=q.strip() or None,
        limit=ARCHIVE_PER_PAGE,
        offset=(page - 1) * ARCHIVE_PER_PAGE,
    )
    pages = max(1, -(-total // ARCHIVE_PER_PAGE))
    topics_facet, sources_facet = repository.archive_facets(session)
    return _templates.TemplateResponse(
        request,
        "archive.html",
        {
            "nav": "archive",
            "rows": rows,
            "total": total,
            "page": min(page, pages),
            "pages": pages,
            "window": _page_window(min(page, pages), pages),
            "first_index": (min(page, pages) - 1) * ARCHIVE_PER_PAGE + 1,
            "last_index": min(min(page, pages) * ARCHIVE_PER_PAGE, total),
            "topic": topic.strip(),
            "source": source.strip(),
            "q": q.strip(),
            "topics_facet": topics_facet,
            "sources_facet": sources_facet,
        },
    )
