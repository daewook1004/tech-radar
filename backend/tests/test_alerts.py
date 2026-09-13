from datetime import date
from types import SimpleNamespace

from app.pipeline.alert import send_alert
from app.pipeline.check_digest import digest_problem
from app.pipeline.deliver import NotificationSender

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
