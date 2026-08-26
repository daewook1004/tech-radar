import logging
import smtplib
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import Digest
from app.pipeline.rank import RankedItem

logger = logging.getLogger(__name__)
_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "output"


def render_digest_text(run_date: date, trend_summary: str, items: list[RankedItem]) -> str:
    lines = [f"=== 오늘의 Tech Radar ({run_date.isoformat()}) ===\n"]
    for rank, (row, rrf_score, is_diversity) in enumerate(items, start=1):
        analysis = row.llm_analysis or {}
        tag = " [다양성 보장]" if is_diversity else ""
        # RRF 원점수(예: 0.0492)는 그대로 보여주면 사람이 체감하기 어려워
        # ×1000만 해서 표시용으로만 스케일업(랭킹 로직에는 영향 없음).
        lines.append(f"{rank}. {row.title} — RRF Score: {rrf_score * 1000:.1f}{tag}")
        lines.append(f"   {analysis.get('summary', '(분석 없음)')}")
        lines.append(f"   왜 중요한가: {analysis.get('why_important', '-')}")
        lines.append(f"   URL: {row.url}\n")
    if trend_summary:
        lines.append("=== 오늘의 주요 흐름 ===")
        lines.append(trend_summary)
    return "\n".join(lines)


class NotificationSender:
    def send(self, subject: str, text: str) -> None:
        raise NotImplementedError


class ConsoleSender(NotificationSender):
    """Gmail 설정이 없을 때의 폴백 — 콘솔에 출력만 한다 (파일 저장은 deliver()가 항상 별도로 함)."""

    def send(self, subject: str, text: str) -> None:
        print(text)


class GmailSMTPSender(NotificationSender):
    def __init__(self, from_addr: str, app_password: str, to_addr: str):
        self.from_addr = from_addr
        self.app_password = app_password
        self.to_addr = to_addr

    def send(self, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg.set_content(text)

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(self.from_addr, self.app_password)
            smtp.send_message(msg)


def _default_sender() -> NotificationSender:
    from app.config import get_settings

    settings = get_settings()
    if settings.gmail_address and settings.gmail_app_password and settings.email_to:
        return GmailSMTPSender(settings.gmail_address, settings.gmail_app_password, settings.email_to)
    logger.warning(".env에 GMAIL_ADDRESS/GMAIL_APP_PASSWORD/EMAIL_TO가 없어 콘솔 출력으로 대체합니다.")
    return ConsoleSender()


def _save_to_file(run_date: date, text: str) -> Path:
    _OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = _OUTPUT_DIR / f"{run_date.isoformat()}.txt"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def deliver(
    session: Session,
    run_date: date,
    trend_summary: str,
    items: list[RankedItem],
    sender: NotificationSender | None = None,
) -> Digest:
    sender = sender or _default_sender()
    text = render_digest_text(run_date, trend_summary, items)

    out_path = _save_to_file(run_date, text)
    logger.info("digest saved to %s", out_path)

    digest = repository.save_digest(session, run_date, trend_summary, items)
    try:
        sender.send(f"오늘의 Tech Radar ({run_date.isoformat()})", text)
        digest.email_sent_at = datetime.utcnow()
        digest.email_status = "sent"
        logger.info("delivered via %s", type(sender).__name__)
    except Exception:
        logger.exception("알림 발송 실패")
        digest.email_status = "failed"
    session.commit()
    return digest
