from datetime import date, datetime

from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import Digest
from app.pipeline.rank import RankedItem


class NotificationSender:
    def send(self, run_date: date, trend_summary: str, items: list[RankedItem]) -> None:
        raise NotImplementedError


class ConsoleEmailSender(NotificationSender):
    """이메일 발송 서비스가 아직 미정(PRD §11)이므로 기본 구현은 콘솔 출력.
    나중에 실제 서비스(Resend/SES 등)로 교체해도 deliver() 호출부는 그대로 둘 수 있게
    NotificationSender 인터페이스로 분리해둠."""

    def send(self, run_date: date, trend_summary: str, items: list[RankedItem]) -> None:
        lines = [f"=== 오늘의 Tech Radar ({run_date.isoformat()}) ===\n"]
        for rank, (row, score, is_diversity) in enumerate(items, start=1):
            analysis = row.llm_analysis or {}
            tag = " [다양성 보장]" if is_diversity else ""
            lines.append(f"{rank}. {row.title} — Score: {score:.1f}{tag}")
            lines.append(f"   {analysis.get('summary', '(분석 없음)')}")
            lines.append(f"   왜 중요한가: {analysis.get('why_important', '-')}")
            lines.append(f"   URL: {row.url}\n")
        if trend_summary:
            lines.append("=== 오늘의 주요 흐름 ===")
            lines.append(trend_summary)
        print("\n".join(lines))


def deliver(
    session: Session,
    run_date: date,
    trend_summary: str,
    items: list[RankedItem],
    sender: NotificationSender | None = None,
) -> Digest:
    sender = sender or ConsoleEmailSender()
    digest = repository.save_digest(session, run_date, trend_summary, items)
    try:
        sender.send(run_date, trend_summary, items)
        digest.email_sent_at = datetime.utcnow()
        digest.email_status = "sent"
    except Exception:
        digest.email_status = "failed"
    session.commit()
    return digest
