from datetime import datetime, timezone

from app.pipeline.dedup import dedup
from app.pipeline.normalize import compute_content_hash, detect_language, normalize, normalize_url
from app.schemas.content import Content


def _make_item(title: str, url: str, text: str = "") -> Content:
    return Content(
        source="geeknews",
        source_type="community",
        title=title,
        text=text,
        url=url,
        collected_at=datetime.now(timezone.utc),
        raw_data={},
    )


def test_normalize_url_strips_tracking_params():
    url = "https://example.com/post/?utm_source=x&id=1"
    assert normalize_url(url) == "https://example.com/post?id=1"


def test_normalize_url_lowercases_scheme_and_host():
    assert normalize_url("https://Example.COM/Path") == "https://example.com/Path"


def test_detect_language_korean():
    assert detect_language("파이토치 새 버전 발표") == "ko"


def test_detect_language_english():
    assert detect_language("PyTorch releases new version") == "en"


def test_compute_content_hash_is_stable():
    h1 = compute_content_hash("https://a.com/x", "Title")
    h2 = compute_content_hash("https://a.com/x", "Title")
    assert h1 == h2


def test_compute_content_hash_differs_for_different_title():
    h1 = compute_content_hash("https://a.com/x", "Title A")
    h2 = compute_content_hash("https://a.com/x", "Title B")
    assert h1 != h2


def test_normalize_sets_language_hash_and_strips_tracking_url():
    item = normalize(_make_item("PyTorch 3.0 발표", "https://a.com/x?utm_source=y"))
    assert item.language == "ko"
    assert item.content_hash
    assert "utm_source" not in item.url


def test_dedup_removes_exact_hash_duplicates():
    item1 = normalize(_make_item("같은 글", "https://a.com/1"))
    item2 = normalize(_make_item("같은 글", "https://a.com/1"))
    assert len(dedup([item1, item2])) == 1


def test_dedup_removes_near_duplicate_titles_across_different_urls():
    item1 = normalize(_make_item("PyTorch 3.0 정식 출시 발표", "https://a.com/1"))
    item2 = normalize(_make_item("PyTorch 3.0 정식 출시 발표!", "https://b.com/2"))
    assert len(dedup([item1, item2])) == 1


def test_dedup_keeps_distinct_items():
    item1 = normalize(_make_item("PyTorch 3.0 발표", "https://a.com/1"))
    item2 = normalize(_make_item("vLLM 신규 기능 공개", "https://b.com/2"))
    assert len(dedup([item1, item2])) == 2
