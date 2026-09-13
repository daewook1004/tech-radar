"""다이제스트가 아예 안 나간 날을 잡아내는 안전망.

run_pipeline의 실패 알림은 파이프라인이 돌기는 했을 때만 뜬다. cron이 멈췄거나 컨테이너가
못 뜨거나 서버가 죽었으면 아무도 알려주지 않는다 — 그래서 발송 예정 시각(07:00 KST)보다
한참 뒤에 다이제스트가 실제로 있는지 따로 확인한다.

호스트 cron: 30 23 * * * (UTC) = 08:30 KST
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app.db import repository
from app.db.models import Digest
from app.db.session import get_session
from app.pipeline.alert import send_alert

_KST = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_digest")


def digest_problem(digest: Digest | None, run_date: date) -> str | None:
    """문제가 없으면 None, 있으면 알림에 쓸 한 줄."""
    if digest is None:
        return f"{run_date} 다이제스트가 없습니다 — 파이프라인이 아예 돌지 않았거나 중간에 멈췄습니다."
    if digest.email_status != "sent":
        return f"{run_date} 다이제스트는 만들어졌지만 메일 발송 상태가 '{digest.email_status}'입니다."
    return None


def run() -> None:
    run_date = datetime.now(_KST).date()
    session = get_session()
    problem = digest_problem(repository.get_digest(session, run_date), run_date)
    if problem is None:
        logger.info("digest ok: %s", run_date)
        return

    logger.warning("digest missing: %s", problem)
    send_alert(
        f"[Tech Radar] 다이제스트가 오지 않았습니다 — {run_date.isoformat()}",
        f"{problem}\n\n"
        "서버에서 확인할 것:\n"
        "  tail -50 /var/log/tech-radar.log\n"
        "  cd /opt/tech-radar && docker compose ps\n"
        "  docker compose run --rm backend python -m app.pipeline.run_pipeline   # 다시 돌리기\n",
    )


if __name__ == "__main__":
    run()
