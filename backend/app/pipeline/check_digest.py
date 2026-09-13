"""다이제스트가 아예 안 나간 날과, 소스 하나가 며칠째 죽어 있는 걸 잡아내는 안전망.

run_pipeline의 알림은 파이프라인이 돌기는 했을 때만 뜬다. cron이 멈췄거나 컨테이너가 못
뜨거나 서버가 죽었으면 아무도 알려주지 않는다. 그리고 수집기 하나가 매일 실패하는 건
실행마다 보면 "흔한 일"이라 안 알리는 쪽이 맞지만, 그게 며칠 이어지면 그 소스는 다이제스트에서
빠진 채로 계속 발송된다 — 조용히 나빠지는 쪽이 더 위험하다.

호스트 cron: 30 23 * * * (UTC) = 08:30 KST
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app.db import repository
from app.db.models import Digest, PipelineRun
from app.db.session import get_session
from app.pipeline.alert import send_alert

_KST = timezone(timedelta(hours=9))
# 이만큼 연속으로 실패한 소스만 알린다. arXiv 429처럼 산발적인 실패(최근 14회 중 2회)는
# 이 문턱을 넘지 않고, 주소가 바뀌어 죽은 피드는 넘는다.
REPEAT_RUNS = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_digest")


def digest_problem(digest: Digest | None, run_date: date) -> str | None:
    """문제가 없으면 None, 있으면 알림에 쓸 한 줄."""
    if digest is None:
        return f"{run_date} 다이제스트가 없습니다 — 파이프라인이 아예 돌지 않았거나 중간에 멈췄습니다."
    if digest.email_status != "sent":
        return f"{run_date} 다이제스트는 만들어졌지만 메일 발송 상태가 '{digest.email_status}'입니다."
    return None


def repeatedly_failing_sources(runs: list[PipelineRun], needed: int = REPEAT_RUNS) -> list[str]:
    """최근 needed번의 실행에서 한 번도 빠짐없이 실패한 수집 소스."""
    if len(runs) < needed:
        return []
    per_run = [
        {f.get("source") for f in (r.failures or []) if f.get("stage") == "collect" and f.get("source")}
        for r in runs[:needed]
    ]
    return sorted(set.intersection(*per_run))


def run() -> None:
    run_date = datetime.now(_KST).date()
    session = get_session()

    problems = []
    missing = digest_problem(repository.get_digest(session, run_date), run_date)
    if missing:
        problems.append(missing)
    stuck = repeatedly_failing_sources(repository.get_recent_pipeline_runs(session, REPEAT_RUNS))
    if stuck:
        problems.append(
            f"{', '.join(stuck)} 소스가 최근 {REPEAT_RUNS}번 연속으로 실패했습니다 — "
            "이 소스는 다이제스트에서 빠진 채로 계속 발송되고 있습니다."
        )

    if not problems:
        logger.info("digest ok: %s", run_date)
        return

    logger.warning("check failed: %s", " / ".join(problems))
    send_alert(
        f"[Tech Radar] {'다이제스트가 오지 않았습니다' if missing else '소스 하나가 계속 실패하고 있습니다'}"
        f" — {run_date.isoformat()}",
        "\n".join(problems) + "\n\n"
        "서버에서 확인할 것:\n"
        "  tail -50 /var/log/tech-radar.log\n"
        "  cd /opt/tech-radar && docker compose ps\n"
        "  docker compose run --rm backend python -m app.pipeline.run_pipeline   # 다시 돌리기\n",
    )


if __name__ == "__main__":
    run()
