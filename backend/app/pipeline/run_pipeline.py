import logging
import sys
from datetime import datetime, timedelta, timezone

import openai

# 서버(컨테이너)는 UTC로 도는데 cron은 한국 시간 아침 배송에 맞춰 22:00 UTC(=KST 07:00,
# 다음날)에 실행된다. date.today()를 그대로 쓰면 서버 시계가 아직 "어제"라 다이제스트
# 날짜가 하루 밀려서 찍힌다(2026-09-04 실측) — run_date는 서버 시간대와 무관하게
# 항상 KST 기준 달력 날짜로 명시적으로 계산한다.
_KST = timezone(timedelta(hours=9))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.collectors.arxiv import ArxivCollector
from app.collectors.geeknews import GeekNewsCollector
from app.collectors.github import GitHubCollector
from app.collectors.hackernews import HackerNewsCollector
from app.collectors.rss import RSSCollector
from app.config import get_settings, get_sources_config
from app.db import repository
from app.db.session import get_session
from app.llm import client as llm_client
from app.pipeline.dedup import dedup
from app.pipeline.deliver import deliver
from app.pipeline.embed_filter import embed_filter
from app.pipeline.llm_analyze import analyze_items
from app.pipeline.llm_score import score_items
from app.pipeline.normalize import normalize
from app.pipeline.rank import rank_and_cutoff, rrf_scores
from app.pipeline.trend_summary import summarize_trends

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_pipeline")

# 새 소스는 이 dict에 팩토리 함수 하나만 추가하면 되고, 아래 파이프라인 로직은 안 바뀐다.
# 소스 하나가 통째로 실패해도(collect()가 예외를 던지면) 다른 소스는 계속 진행된다(§run() 참조).
COLLECTOR_FACTORIES = {
    "geeknews": lambda cfg: GeekNewsCollector(rss_url=cfg["geeknews"]["rss_url"]),
    "hackernews": lambda cfg: HackerNewsCollector(min_points=cfg["hackernews"]["min_points"]),
    "github": lambda cfg: GitHubCollector(
        watch_repos=cfg["github"]["watch_repos"],
        trending_keywords=cfg["github"]["trending_approx"]["keywords"],
        created_within_days=cfg["github"]["trending_approx"]["created_within_days"],
        min_stars=cfg["github"]["trending_approx"]["min_stars"],
        github_token=get_settings().github_token,
    ),
    "arxiv": lambda cfg: ArxivCollector(categories=cfg["arxiv"]["categories"], keywords=cfg["arxiv"]["keywords"]),
    "rss": lambda cfg: RSSCollector(feeds=cfg["rss_blogs"]),
}

# LLM 정밀분석(2차) 대상으로 넘길 상위 후보 수 — 1차 스코어링을 통과한 것 중 이만큼만 Sonnet 호출
ANALYZE_TOP_N = 20


def run() -> None:
    run_date = datetime.now(_KST).date()
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
        # 정밀분석(top 20) 선정도 최종 랭킹과 동일한 weighted RRF를 재사용한다 — 예전엔
        # relevance*10+importance+novelty처럼 별도의 손튜닝 가중합을 썼는데, 최종 랭킹과
        # 똑같은 스케일 불일치 문제로 relevance 높은 기업 블로그 글이 이 단계에서부터
        # 탈락하는 걸 실측으로 확인함(2026-08-26) — 최종 랭킹의 cap이 아무리 좋아도
        # 애초에 분석 후보에 못 들면 손 쓸 방법이 없었음.
        pre_rrf = rrf_scores(scored_rows)
        top_rows = sorted(scored_rows, key=lambda r: pre_rrf[r.id], reverse=True)[:ANALYZE_TOP_N]
        analyzed_rows = analyze_items(session, top_rows, run_date)
        trend_text = summarize_trends(session, passed_rows, run_date)

        ranked = rank_and_cutoff(session, analyzed_rows, run_date)
        deliver(session, run_date, trend_text, ranked, all_items=content_rows)

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
