import logging
import sys
from datetime import date

import openai

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.collectors.geeknews import GeekNewsCollector
from app.config import get_sources_config
from app.db import repository
from app.db.session import get_session
from app.llm import client as llm_client
from app.pipeline.dedup import dedup
from app.pipeline.deliver import deliver
from app.pipeline.embed_filter import embed_filter
from app.pipeline.llm_analyze import analyze_items
from app.pipeline.llm_score import score_items
from app.pipeline.normalize import normalize
from app.pipeline.rank import rank_and_cutoff
from app.pipeline.trend_summary import summarize_trends

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_pipeline")

# 이번 세션(Walking Skeleton)은 GeekNews만. 다음 세션에서 hackernews/github/arxiv/rss 추가 —
# 새 소스는 이 dict에 팩토리 함수 하나만 추가하면 되고, 아래 파이프라인 로직은 안 바뀐다.
COLLECTOR_FACTORIES = {
    "geeknews": lambda cfg: GeekNewsCollector(rss_url=cfg["geeknews"]["rss_url"]),
}

# LLM 정밀분석(2차) 대상으로 넘길 상위 후보 수 — 1차 스코어링을 통과한 것 중 이만큼만 Sonnet 호출
ANALYZE_TOP_N = 20


def _pre_llm_rank(row) -> float:
    scores = row.scores or {}
    return scores.get("relevance", 0) * 10 + scores.get("importance", 0) + scores.get("novelty", 0)


def run() -> None:
    run_date = date.today()
    session = get_session()
    run = repository.start_pipeline_run(session, run_date)

    sources_cfg = get_sources_config()
    failures: list[dict] = []
    collected = []

    for name, factory in COLLECTOR_FACTORIES.items():
        try:
            items = factory(sources_cfg).collect()
            logger.info("collected %d items from %s", len(items), name)
            collected.extend(items)
        except Exception as e:  # 소스 단위 실패 격리 — 한 소스가 죽어도 나머지는 계속 진행
            logger.exception("collector %s failed", name)
            failures.append({"stage": "collect", "source": name, "error": str(e)})

    normalized = [normalize(item) for item in collected]
    deduped = dedup(normalized)
    logger.info("after dedup: %d/%d items", len(deduped), len(normalized))

    content_rows = repository.upsert_content_batch(session, deduped)
    logger.info("persisted %d content rows", len(content_rows))

    passed_rows = embed_filter(session, content_rows)
    logger.info("passed embed filter: %d/%d items", len(passed_rows), len(content_rows))

    status = "success"
    try:
        llm_client.require_api_key()

        scored_rows = score_items(session, passed_rows, run_date)
        top_rows = sorted(scored_rows, key=_pre_llm_rank, reverse=True)[:ANALYZE_TOP_N]
        analyzed_rows = analyze_items(session, top_rows, run_date)
        trend_text = summarize_trends(session, passed_rows, run_date)

        ranked = rank_and_cutoff(session, analyzed_rows, run_date)
        deliver(session, run_date, trend_text, ranked)

        if failures:
            status = "partial_failure"
    except llm_client.MissingAPIKeyError as e:
        logger.warning("LLM 단계 건너뜀: %s", e)
        failures.append({"stage": "llm", "source": None, "error": str(e)})
        status = "partial_failure"
    except llm_client.BudgetExceededError as e:
        logger.warning("예산 초과로 파이프라인 중단: %s", e)
        failures.append({"stage": "llm", "source": None, "error": str(e)})
        status = "partial_failure"
    except openai.OpenAIError as e:
        # 인증 실패/rate limit/타임아웃 등 OpenAI API 호출 자체가 실패한 경우 —
        # PRD §9: 조용히 죽지 않고 실패로 기록한 뒤 그때까지의 결과(있다면)로 계속.
        logger.exception("OpenAI API 호출 실패")
        failures.append({"stage": "llm", "source": None, "error": str(e)})
        status = "partial_failure"

    run.failures = failures
    repository.finish_pipeline_run(
        session,
        run,
        status,
        {
            "collected": len(collected),
            "deduped": len(deduped),
            "passed_filter": len(passed_rows),
            "failure_count": len(failures),
        },
    )
    logger.info("pipeline finished: status=%s failures=%s", status, failures)


if __name__ == "__main__":
    run()
