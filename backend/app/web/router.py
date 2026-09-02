import re
from datetime import date
from pathlib import Path

import markdown as _markdown
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session

from app.db import repository
from app.db.session import get_db
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


_templates.env.filters["markdown"] = _render_markdown
_templates.env.filters["strip_markdown"] = _strip_markdown


@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/digests")


@router.get("/digests")
def digest_list(request: Request, session: Session = Depends(get_db)):
    digests = repository.list_digests(session)
    return _templates.TemplateResponse(
        request, "digest_list.html", {"digests": digests}
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
            "digest": digest,
            "items": items,
            "by_source": dict(sorted(by_source.items())),
            "selected_ids": selected_ids,
        },
    )
