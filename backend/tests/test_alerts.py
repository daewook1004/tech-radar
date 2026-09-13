from datetime import date
from types import SimpleNamespace

from app.pipeline.alert import send_alert
from app.pipeline.check_digest import digest_problem, repeatedly_failing_sources
from app.pipeline.deliver import NotificationSender
from app.pipeline.run_pipeline import alert_reason

RUN_DATE = date(2026, 9, 12)


class _BrokenSender(NotificationSender):
    def send(self, subject: str, text: str) -> None:
        raise RuntimeError("smtp down")


def test_send_alert_never_raises_at_the_caller():
    # 알림이 실패해도 부르는 쪽(파이프라인 crash 처리)이 더 망가지면 안 된다
    assert send_alert("subject", "text", sender=_BrokenSender()) is False


def test_send_alert_reports_success():
    sent = []
    sender = SimpleNamespace(send=lambda subject, text: sent.append(subject))
    assert send_alert("subject", "text", sender=sender) is True
    assert sent == ["subject"]


def test_digest_problem_flags_a_missing_digest():
    # 2026-09-12·13: 파이프라인이 죽어 digest 행 자체가 없었다
    assert "없습니다" in digest_problem(None, RUN_DATE)


def test_digest_problem_flags_a_digest_that_never_got_mailed():
    problem = digest_problem(SimpleNamespace(email_status="failed"), RUN_DATE)
    assert problem and "failed" in problem


def test_digest_problem_stays_quiet_when_the_mail_went_out():
    assert digest_problem(SimpleNamespace(email_status="sent"), RUN_DATE) is None


def _run(*failures: dict) -> SimpleNamespace:
    return SimpleNamespace(failures=list(failures))


def _collect_failure(source: str) -> dict:
    return {"stage": "collect", "source": source, "error": "429 Client Error"}


def test_alert_reason_stays_quiet_for_one_flaky_collector():
    # arXiv 429는 흔하고 다이제스트는 정상적으로 나간다 — 이걸 알리면 알림을 안 읽게 된다
    assert alert_reason([_collect_failure("arxiv")], collector_count=5) is None


def test_alert_reason_speaks_up_when_every_collector_failed():
    failures = [_collect_failure(s) for s in ("geeknews", "hackernews", "github", "arxiv", "rss")]
    reason = alert_reason(failures, collector_count=5)
    assert reason and "모든 수집기" in reason


def test_alert_reason_speaks_up_for_a_failure_outside_collection():
    reason = alert_reason([{"stage": "fetch_body", "source": None, "error": "NUL bytes"}], collector_count=5)
    assert reason and "fetch_body" in reason


def test_repeatedly_failing_sources_needs_every_recent_run():
    runs = [_run(_collect_failure("arxiv")) for _ in range(3)]
    assert repeatedly_failing_sources(runs) == ["arxiv"]

    runs[1] = _run()  # 가운데 실행은 멀쩡했다면 연속 실패가 아니다
    assert repeatedly_failing_sources(runs) == []


def test_repeatedly_failing_sources_waits_for_enough_history():
    assert repeatedly_failing_sources([_run(_collect_failure("arxiv"))] * 2) == []
