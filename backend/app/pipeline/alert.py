"""파이프라인이 조용히 죽지 않게 하는 알림.

2026-09-12·13에 본문 저장 오류로 파이프라인이 채점 전에 멈췄는데, 이틀 동안 아무 신호도
없었다 — "오늘 메일이 안 왔네"가 유일한 단서였다. 실패도 다이제스트와 같은 경로(Gmail
SMTP, 설정이 없으면 콘솔)로 알린다. 알림 전용 채널(SNS 등)을 따로 두지 않은 이유는,
받는 사람이 한 명이고 이미 매일 보는 편지함이 가장 확실한 도착지이기 때문이다.
"""
import logging

from app.pipeline.deliver import NotificationSender, default_sender

logger = logging.getLogger(__name__)


def send_alert(subject: str, text: str, sender: NotificationSender | None = None) -> bool:
    """알림 발송이 실패해도 부르는 쪽을 더 망가뜨리지 않는다 — 로그만 남기고 False를 준다."""
    try:
        (sender or default_sender()).send(subject, text)
        logger.info("alert sent: %s", subject)
        return True
    except Exception:
        logger.exception("알림 발송 실패: %s", subject)
        return False
