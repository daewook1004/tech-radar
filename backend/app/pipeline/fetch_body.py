import logging
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests
from sqlalchemy.orm import Session

from app.db.models import Content as ContentRow

logger = logging.getLogger(__name__)

# HN API는 링크 글의 본문을 주지 않고, techblogposts 같은 애그리게이터 피드는 제목·링크만
# 준다. 그래서 임베딩 필터를 통과한 글의 1/3(2026-09-03~09-11, 400건 중 129건)이 본문 없이
# LLM 채점에 들어갔고, LLM은 "본문이 없어 확인할 수 없다"며 세 점수를 일관되게 깎았다 —
# 같은 글에서 본문만 빼고 다시 채점하면 중요도가 평균 1.75점 떨어졌다(재현 오차의 약 6배).
# 원문은 실제로 있다: 링크를 열어보면 11개 중 8개에서 본문이 그대로 나왔다.
#
# 이보다 짧은 본문은 LLM에게 판단을 맡길 근거가 못 된다고 본다. 원문을 가져와도 여전히
# 짧으면 LLM 채점·정밀분석을 건너뛰고 '모름'으로 둔다(llm_score.py, llm_analyze.py, rank.py).
MIN_BODY_CHARS = 200
# 정밀분석이 읽는 최대 길이(llm_analyze.py의 [:4000])와 맞춘다 — 더 저장해도 아무도 안 읽는다.
MAX_BODY_CHARS = 4000

_TIMEOUT = (5, 10)  # (연결, 읽기) 초 — 느린 사이트 하나가 파이프라인을 붙잡지 않게
_MAX_BYTES = 2_000_000
_MAX_WORKERS = 8
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tech-radar/1.0; +https://github.com/daewook1004/tech-radar)"}
# Ask/Show HN은 url이 HN 댓글 페이지라 열어도 본문이 아니라 댓글이 나온다
_SKIP_PREFIXES = ("https://news.ycombinator.com/",)


def has_body(row: ContentRow) -> bool:
    return len((row.text or "").strip()) >= MIN_BODY_CHARS


class _BodyExtractor(HTMLParser):
    """문단을 모으되 script/style과 nav·header·footer 같은 페이지 틀은 건너뛴다.

    본문 영역(<article>, <main>, GeekNews의 section#topic_contents)이 있으면 그 안의 <p>와
    <li>를 따로 모은다. GeekNews 요약과 GitHub README는 글머리표 위주라 <p>만 보면 본문은 놓치고
    본문 영역 밖의 댓글을 주워 온다(2026-09-11 실측). arXiv 초록 페이지처럼 citation_abstract
    메타가 있으면 그게 가장 정확한 본문이다."""

    _IGNORED = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside"}
    _ROOT_TAGS = {"article", "main", "section"}
    _ROOT_IDS = {"topic_contents"}
    # 본문 영역 밖에서 흔히 잡히는 사이트 공통 문구
    _BOILERPLATE = {
        "There was an error while loading. Please reload this page.",  # GitHub
        "Notifications You must be signed in to change notification settings",  # GitHub
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._root_stack: list[bool] = []  # 열린 article/main/section마다 본문 영역인지
        self._block: str | None = None  # 지금 모으고 있는 문단 태그("p"/"li")
        self._block_in_root = False
        self._buf: list[str] = []
        self.root_blocks: list[str] = []
        self.paragraphs: list[str] = []
        self.citation_abstract = ""

    def _in_root(self) -> bool:
        return any(self._root_stack)

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        # "Share", "Read more" 같은 조각은 버린다
        if len(text) > 30 and text not in self._BOILERPLATE:
            if self._block_in_root:
                self.root_blocks.append(text)
            if self._block == "p":
                self.paragraphs.append(text)
        self._buf = []
        self._block = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in self._IGNORED:
            self._ignored_depth += 1
        elif tag in self._ROOT_TAGS:
            self._root_stack.append(tag != "section" or attr.get("id") in self._ROOT_IDS)
        elif tag in ("p", "li") and not self._ignored_depth:
            if tag == "li" and not self._in_root():
                return  # 본문 영역 밖의 목록은 메뉴·링크 모음일 때가 많다
            if self._block:  # 닫히지 않은 문단 뒤에 새 문단이 오면 앞 문단을 먼저 끊는다
                self._flush()
            self._block = tag
            self._block_in_root = self._in_root()
        elif tag == "meta" and attr.get("name") == "citation_abstract" and attr.get("content"):
            self.citation_abstract = attr["content"]

    def handle_endtag(self, tag):
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._ROOT_TAGS and self._root_stack:
            if self._block and self._block_in_root:
                self._flush()
            self._root_stack.pop()
        elif tag == self._block:
            self._flush()

    def handle_data(self, data):
        if self._block and not self._ignored_depth:
            self._buf.append(data)

    def close(self):
        super().close()
        if self._block:  # 문서 끝까지 닫히지 않은 마지막 문단
            self._flush()


def extract_body(html: str) -> str:
    """페이지 HTML에서 본문 텍스트를 뽑는다. 우선순위: citation_abstract → 본문 영역 → 페이지 전체 <p>.
    MIN_BODY_CHARS보다 짧으면(자바스크립트로만 그려지는 페이지라 틀만 남은 경우 등) 본문을
    못 찾은 것으로 보고 빈 문자열을 돌려준다."""
    parser = _BodyExtractor()
    parser.feed(html)
    parser.close()
    candidates = [" ".join(parser.citation_abstract.split()), "\n".join(parser.root_blocks), "\n".join(parser.paragraphs)]
    body = next((c for c in candidates if len(c) >= MIN_BODY_CHARS), "")
    return body[:MAX_BODY_CHARS]


def _fetch(url: str) -> str:
    try:
        with requests.get(url, timeout=_TIMEOUT, headers=_HEADERS, stream=True) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type:
                return ""  # PDF 등은 파싱하지 않는다
            raw = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                raw += chunk
                if len(raw) >= _MAX_BYTES:
                    break
            # charset이 헤더에 없으면 requests가 ISO-8859-1로 가정해 한글이 깨진다
            charset = resp.encoding if "charset=" in content_type.lower() else "utf-8"
        return extract_body(bytes(raw).decode(charset or "utf-8", errors="replace"))
    except Exception as e:  # 원문 하나가 실패해도 파이프라인은 계속 — 그 글은 '모름'으로 처리된다
        logger.info("body fetch failed for %s: %s", url, e)
        return ""


def fill_missing_bodies(session: Session, rows: list[ContentRow]) -> tuple[int, int]:
    """본문이 MIN_BODY_CHARS보다 짧은 글의 원문을 가져와 text에 덧붙인다.
    임베딩 relevance는 본문 길이와 무관해서(필터 풀 안 상관 +0.04) 필터를 통과한 글에만 해도 된다 —
    하루 수백 건이 아니라 수십 건만 요청한다. 반환값은 (채운 수, 여전히 본문 없는 수)."""
    targets = [r for r in rows if not has_body(r) and r.url.startswith("http") and not r.url.startswith(_SKIP_PREFIXES)]
    if targets:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            bodies = list(ex.map(_fetch, [r.url for r in targets]))
    else:
        bodies = []

    filled = 0
    for row, body in zip(targets, bodies):
        if not body:
            continue
        existing = (row.text or "").strip()
        row.text = f"{existing}\n\n{body}" if existing else body
        row.raw_data = {**(row.raw_data or {}), "fetched_body_chars": len(body)}
        filled += 1
    session.commit()
    return filled, sum(1 for r in rows if not has_body(r))
