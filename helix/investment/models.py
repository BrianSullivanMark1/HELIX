from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RISK_LEVELS = ("conservative", "balanced", "growth", "aggressive")


@dataclass(frozen=True)
class InvestmentProfile:
    monthly_income: float
    monthly_expenses: float
    cash_savings: float
    debt_total: float
    monthly_debt_payment: float
    current_investments: float
    target_emergency_months: int
    risk_tolerance: str
    primary_goal: str
    goal_amount: float
    goal_years: int
    expected_annual_return: float

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "InvestmentProfile":
        return cls(
            monthly_income=float(record["monthly_income"]),
            monthly_expenses=float(record["monthly_expenses"]),
            cash_savings=float(record["cash_savings"]),
            debt_total=float(record["debt_total"]),
            monthly_debt_payment=float(record["monthly_debt_payment"]),
            current_investments=float(record["current_investments"]),
            target_emergency_months=int(record["target_emergency_months"]),
            risk_tolerance=str(record["risk_tolerance"]).lower(),
            primary_goal=str(record["primary_goal"]),
            goal_amount=float(record["goal_amount"]),
            goal_years=int(record["goal_years"]),
            expected_annual_return=float(record["expected_annual_return"]),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "monthly_income": self.monthly_income,
            "monthly_expenses": self.monthly_expenses,
            "cash_savings": self.cash_savings,
            "debt_total": self.debt_total,
            "monthly_debt_payment": self.monthly_debt_payment,
            "current_investments": self.current_investments,
            "target_emergency_months": self.target_emergency_months,
            "risk_tolerance": self.risk_tolerance,
            "primary_goal": self.primary_goal,
            "goal_amount": self.goal_amount,
            "goal_years": self.goal_years,
            "expected_annual_return": self.expected_annual_return,
        }
