import os
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_pipeline_settings
from app.db.models import CostLedger

# OpenAI 공식 가격 페이지(developers.openai.com/api/docs/pricing) 2026-08-26 확인 기준, USD per token.
# gpt-5.6 계열(sol/terra/luna)이 현재 모델 카탈로그의 플래그십 — PRD §5.4 참조.
_PRICING = {
    "gpt-5.6-luna": {"input": Decimal("0.20"), "output": Decimal("1.20")},
    "gpt-5.6-sol": {"input": Decimal("4.00"), "output": Decimal("20.00")},  # 2026-11-21까지 프로모션가(원가 $5/$30)
}
_PER_TOKEN = Decimal("1000000")


class MissingAPIKeyError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


def require_api_key() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise MissingAPIKeyError(
            "OPENAI_API_KEY가 설정되지 않았습니다. .env에 키를 추가한 뒤 다시 실행하세요."
        )


def _cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    price = _PRICING[model]
    return (Decimal(input_tokens) / _PER_TOKEN * price["input"]) + (
        Decimal(output_tokens) / _PER_TOKEN * price["output"]
    )


def check_budget(session: Session, run_date: date) -> None:
    """매 LLM 호출 전에 호출 — PRD §9 비용 폭주 방지 안전장치."""
    settings = get_pipeline_settings()["budget"]

    daily_total = session.scalar(
        select(func.coalesce(func.sum(CostLedger.cost_usd), 0)).where(CostLedger.run_date == run_date)
    )
    if float(daily_total) >= settings["daily_max_usd"]:
        raise BudgetExceededError(f"일일 예산 초과: ${daily_total} >= ${settings['daily_max_usd']}")

    month_start = run_date.replace(day=1)
    monthly_total = session.scalar(
        select(func.coalesce(func.sum(CostLedger.cost_usd), 0)).where(CostLedger.run_date >= month_start)
    )
    if float(monthly_total) >= settings["monthly_max_usd"]:
        raise BudgetExceededError(f"월 예산 초과: ${monthly_total} >= ${settings['monthly_max_usd']}")


def record_cost(
    session: Session, run_date: date, stage: str, model: str, input_tokens: int, output_tokens: int
) -> None:
    session.add(
        CostLedger(
            run_date=run_date,
            stage=stage,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_cost_usd(model, input_tokens, output_tokens),
        )
    )
    session.commit()
