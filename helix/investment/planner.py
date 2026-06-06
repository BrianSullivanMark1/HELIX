from __future__ import annotations

from dataclasses import dataclass

from helix.investment.models import InvestmentProfile


ALLOCATION_MODELS = {
    "conservative": {
        "cash": 15,
        "bonds": 50,
        "broad_us_equity": 25,
        "international_equity": 10,
    },
    "balanced": {
        "cash": 10,
        "bonds": 30,
        "broad_us_equity": 45,
        "international_equity": 15,
    },
    "growth": {
        "cash": 5,
        "bonds": 15,
        "broad_us_equity": 60,
        "international_equity": 20,
    },
    "aggressive": {
        "cash": 5,
        "bonds": 5,
        "broad_us_equity": 65,
        "international_equity": 25,
    },
}


@dataclass(frozen=True)
class InvestmentBriefing:
    profile_exists: bool
    monthly_surplus: float = 0.0
    emergency_target: float = 0.0
    emergency_gap: float = 0.0
    investable_cash_now: float = 0.0
    monthly_investment_target: float = 0.0
    projected_goal_value: float = 0.0
    required_monthly_contribution: float = 0.0
    allocation: dict[str, int] | None = None
    next_action: str = ""


def build_briefing(record: dict | None) -> InvestmentBriefing:
    if not record:
        return InvestmentBriefing(
            profile_exists=False,
            next_action="Create your investment profile: python main.py investment profile",
        )

    profile = InvestmentProfile.from_record(record)
    monthly_surplus = profile.monthly_income - profile.monthly_expenses - profile.monthly_debt_payment
    emergency_target = (
        profile.monthly_expenses + profile.monthly_debt_payment
    ) * profile.target_emergency_months
    emergency_gap = max(0.0, emergency_target - profile.cash_savings)
    investable_cash_now = max(0.0, profile.cash_savings - emergency_target)
    monthly_investment_target = _monthly_investment_target(monthly_surplus, emergency_gap)
    projected_goal_value = project_future_value(
        starting_balance=profile.current_investments,
        monthly_contribution=monthly_investment_target,
        annual_return=profile.expected_annual_return,
        years=profile.goal_years,
    )
    required_monthly_contribution_value = required_monthly_contribution(
        target_value=profile.goal_amount,
        starting_balance=profile.current_investments,
        annual_return=profile.expected_annual_return,
        years=profile.goal_years,
    )
    allocation = ALLOCATION_MODELS.get(profile.risk_tolerance, ALLOCATION_MODELS["balanced"])

    return InvestmentBriefing(
        profile_exists=True,
        monthly_surplus=monthly_surplus,
        emergency_target=emergency_target,
        emergency_gap=emergency_gap,
        investable_cash_now=investable_cash_now,
        monthly_investment_target=monthly_investment_target,
        projected_goal_value=projected_goal_value,
        required_monthly_contribution=required_monthly_contribution_value,
        allocation=allocation,
        next_action=_next_action(monthly_surplus, emergency_gap, investable_cash_now),
    )


def project_future_value(
    starting_balance: float,
    monthly_contribution: float,
    annual_return: float,
    years: int,
) -> float:
    months = max(0, years * 12)
    if months == 0:
        return starting_balance

    monthly_rate = annual_return / 12
    if monthly_rate == 0:
        return starting_balance + monthly_contribution * months

    growth = (1 + monthly_rate) ** months
    contribution_value = monthly_contribution * ((growth - 1) / monthly_rate)
    return starting_balance * growth + contribution_value


def required_monthly_contribution(
    target_value: float,
    starting_balance: float,
    annual_return: float,
    years: int,
) -> float:
    months = max(0, years * 12)
    if months == 0:
        return max(0.0, target_value - starting_balance)

    monthly_rate = annual_return / 12
    if monthly_rate == 0:
        return max(0.0, (target_value - starting_balance) / months)

    growth = (1 + monthly_rate) ** months
    future_starting_balance = starting_balance * growth
    annuity_factor = (growth - 1) / monthly_rate
    return max(0.0, (target_value - future_starting_balance) / annuity_factor)


def render_briefing(briefing: InvestmentBriefing) -> str:
    if not briefing.profile_exists:
        return "\n".join(
            [
                "HELIX Investment Briefing",
                "Status: profile missing",
                f"Next action: {briefing.next_action}",
            ]
        )

    allocation = briefing.allocation or {}
    allocation_text = ", ".join(
        f"{name.replace('_', ' ')} {pct}%" for name, pct in allocation.items()
    )

    return "\n".join(
        [
            "HELIX Investment Briefing",
            f"Monthly surplus: {_money(briefing.monthly_surplus)}",
            f"Emergency target: {_money(briefing.emergency_target)}",
            f"Emergency gap: {_money(briefing.emergency_gap)}",
            f"Investable cash now: {_money(briefing.investable_cash_now)}",
            f"Monthly investment target: {_money(briefing.monthly_investment_target)}",
            f"Projected goal value: {_money(briefing.projected_goal_value)}",
            f"Required monthly contribution: {_money(briefing.required_monthly_contribution)}",
            f"Allocation model: {allocation_text}",
            f"Next action: {briefing.next_action}",
            "Note: planning output only, not financial advice.",
        ]
    )


def _monthly_investment_target(monthly_surplus: float, emergency_gap: float) -> float:
    if monthly_surplus <= 0:
        return 0.0
    if emergency_gap > 0:
        return round(monthly_surplus * 0.2, 2)
    return round(monthly_surplus * 0.8, 2)


def _next_action(monthly_surplus: float, emergency_gap: float, investable_cash_now: float) -> str:
    if monthly_surplus <= 0:
        return "Stabilize cash flow before adding investment risk."
    if emergency_gap > 0:
        return "Build emergency reserves first; keep investing small and automatic."
    if investable_cash_now > 0:
        return "Deploy excess cash according to allocation model after review."
    return "Automate the monthly investment target and review watchlist weekly."


def _money(value: float) -> str:
    return f"${value:,.2f}"
