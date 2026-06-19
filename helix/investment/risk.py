"""Real-time risk monitor — pure evaluation of a portfolio against survival guardrails.

This is the pure-core counterpart to the live risk panel (the I/O — polling Alpaca, drawing the
dashboard, speaking the alert — lives at the UI edge in `qt_app.py`). Given a `PortfolioSnapshot`
(from `autopilot.portfolio_snapshot`), `evaluate_risk()` returns a `RiskReport` describing margin
usage, position concentration, and any tripped alerts. No I/O, no Qt — unit-testable on its own.

Three watched conditions (the §13 risk controls act on the engine; this watches the *result*):
  1. **Margin** — cash has gone negative, i.e. borrowed money is funding positions.
  2. **Concentration** — a single position exceeds `concentration_pct` (default 20%) of equity.
  3. **Drawdown** — an open position's loss exceeds `loss_threshold_pct` (default 15%, configurable).
"""
from __future__ import annotations

from dataclasses import dataclass

# Settings key for the user-configurable open-loss alert threshold (percent, e.g. 15.0).
RISK_LOSS_THRESHOLD_SETTING = "risk_loss_threshold_pct"

DEFAULT_LOSS_THRESHOLD_PCT = 15.0   # alert when an open position is down more than this
DEFAULT_CONCENTRATION_PCT = 20.0    # alert when one name is more than this share of equity


@dataclass(frozen=True)
class RiskAlert:
    """One tripped condition. `symbol` is "" for account-level alerts (e.g. margin)."""

    kind: str       # "margin" | "concentration" | "loss"
    symbol: str
    message: str
    severity: str = "warning"  # "warning" | "critical"


@dataclass(frozen=True)
class ConcentrationRow:
    symbol: str
    market_value: float
    pct: float  # share of equity, percent


@dataclass(frozen=True)
class RiskReport:
    equity: float
    cash: float
    margin_used: float            # dollars of margin in use = max(0, -cash)
    margin_pct: float             # margin_used as a percent of equity
    concentration: list           # list[ConcentrationRow], largest first
    alerts: list                  # list[RiskAlert]

    @property
    def ok(self) -> bool:
        return not self.alerts

    @property
    def critical(self) -> bool:
        return any(a.severity == "critical" for a in self.alerts)


def evaluate_risk(
    snapshot,
    loss_threshold_pct: float = DEFAULT_LOSS_THRESHOLD_PCT,
    concentration_pct: float = DEFAULT_CONCENTRATION_PCT,
) -> RiskReport:
    """Score a `PortfolioSnapshot` against the three guardrails. Pure — safe to call every refresh."""
    equity = float(snapshot.equity or 0.0)
    cash = float(snapshot.cash or 0.0)

    margin_used = max(0.0, -cash)
    margin_pct = round(margin_used / equity * 100.0, 1) if equity > 0 else 0.0

    concentration: list = []
    for pos in snapshot.positions or []:
        pct = round(pos.market_value / equity * 100.0, 1) if equity > 0 else 0.0
        concentration.append(ConcentrationRow(pos.symbol, pos.market_value, pct))
    concentration.sort(key=lambda row: row.market_value, reverse=True)

    alerts: list = []
    if margin_used > 0:
        alerts.append(
            RiskAlert(
                "margin",
                "",
                f"Cash is negative — ${margin_used:,.0f} of margin in use "
                f"({margin_pct:.0f}% of equity).",
                "critical",
            )
        )
    for row in concentration:
        if row.pct > concentration_pct:
            alerts.append(
                RiskAlert(
                    "concentration",
                    row.symbol,
                    f"{row.symbol} is {row.pct:.0f}% of the portfolio — over the "
                    f"{concentration_pct:.0f}% concentration limit.",
                    "warning",
                )
            )
    for pos in snapshot.positions or []:
        # unrealized_plpc is already a percent (portfolio_snapshot multiplies by 100).
        if pos.unrealized_plpc <= -abs(loss_threshold_pct):
            alerts.append(
                RiskAlert(
                    "loss",
                    pos.symbol,
                    f"{pos.symbol} is down {abs(pos.unrealized_plpc):.0f}% — past the "
                    f"{loss_threshold_pct:.0f}% open-loss limit.",
                    "warning",
                )
            )
    return RiskReport(
        equity=round(equity, 2),
        cash=round(cash, 2),
        margin_used=round(margin_used, 2),
        margin_pct=margin_pct,
        concentration=concentration,
        alerts=alerts,
    )


def alert_speech(report: RiskReport) -> str:
    """A single natural-language line HELIX can speak aloud, or "" when nothing is wrong."""
    if report.ok:
        return ""
    n = len(report.alerts)
    head = "Risk alert" if n == 1 else f"{n} risk alerts"
    bodies = [a.message.rstrip(".") for a in report.alerts]
    return f"{head}, sir. " + ". ".join(bodies) + "."


def alert_signature(report: RiskReport) -> frozenset:
    """A stable key for the current alert set, so the UI speaks only when the alerts change
    (not on every poll while the same condition persists)."""
    return frozenset((a.kind, a.symbol) for a in report.alerts)
