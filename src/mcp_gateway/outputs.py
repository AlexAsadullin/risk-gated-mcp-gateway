"""Structured outputs of the contract-6.6 MCP tools. Each becomes the tool's JSON ``outputSchema``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_gateway.contracts import SCHEMA_VERSION, Account, LeakageReport, Order, Position, Verdict
from mcp_gateway.risk import CheckResult
from mcp_gateway.settings import BudgetLimits
from mcp_gateway.trials import Destination


class ToolOutput(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION


class ClockResult(ToolOutput):
    session_id: str
    agent_id: str
    mode: str
    known_at: str = Field(description="Every data, feature, forecast and backtest call is answered as of this.")
    known_at_source: Literal["session", "wall"] = Field(
        description="'session': set by the operator, cannot be moved by the agent; 'wall': real time."
    )
    status: str


class DataResult(ToolOutput):
    dataset: str
    start: str
    end: str
    known_at: str
    clamped: bool = Field(description="True if the requested end was cut back to known_at.")
    fingerprint: str | None = Field(description="I2 dataset fingerprint; reproduces this exact answer.")
    n_rows: int = Field(description="Rows the store returned before truncation.")
    truncated: bool = Field(description="True if only the latest max rows are included.")
    dropped_future_rows: int = Field(description="Rows discarded because knowledge_time > known_at.")
    leakage_flags: list[str] = Field(description="Kinds I2 flagged on this answer (X-Leakage-Flag), if any.")
    rows: list[dict[str, Any]]


class MarketDataResult(DataResult):
    venue: str
    symbol: str
    channel: str


class FeaturesResult(DataResult):
    names: list[str]
    symbols: list[str]


class ForecastPoint(BaseModel):
    symbol: str
    horizon: int = Field(description="Steps ahead, in the model's bar size.")
    mean: float = Field(description="Forecast mean return over the horizon (fraction, 0.01 = 1%).")
    quantiles: dict[str, float]


class ForecastResult(ToolOutput):
    model: str
    horizon: int
    known_at: str
    model_cutoff: str | None
    forecasts: list[ForecastPoint]


class BacktestResult(ToolOutput):
    run_id: str
    strategy: str
    ledger_strategy_id: str
    backtest_trial_id: str | None = Field(description="Trial id I5 recorded for the run, if any.")
    gateway_trial_id: str = Field(
        description="Trial id of this idea in the Research Ledger (S6); I5's own trial when logged_to is 'i5'."
    )
    logged_to: Destination
    known_at: str
    start: str
    end: str
    fidelity: str
    metrics: dict[str, Any]
    fidelity_report: dict[str, Any] | None


class VerdictResult(ToolOutput):
    strategy_id: str
    ledger_strategy_id: str
    verdict: Verdict
    llm_driven: bool = Field(description="True if this session reported LLM usage; then S7 must audit it.")
    leakage_status: Literal["not_required", "clean", "flagged", "not_audited"]
    leakage_reports: list[LeakageReport]
    promotable: bool = Field(description="Verdict passed and, if LLM-driven, the leakage audit is clean.")
    reasons: list[str]


class OrderResult(ToolOutput):
    order: Order
    routed_to: Literal["i6", "i7"]
    ledger_strategy_id: str
    notional: float | None = Field(description="qty * price in quote currency (reference price for market).")
    reference_price: float | None
    checks: list[CheckResult]
    gateway_trial_id: str
    logged_to: Destination


class CancelResult(ToolOutput):
    order: Order
    routed_to: Literal["i6", "i7"]


class PositionsResult(ToolOutput):
    account_id: str
    positions: list[Position]


class AccountResult(ToolOutput):
    account: Account
    peak_equity: float | None
    drawdown: float | None = Field(description="Fractional drawdown from peak equity (0.05 = 5%).")
    max_drawdown: float


class UsageTotals(BaseModel):
    tokens_in: int
    tokens_out: int
    token_cost_usd: float
    latency_total_ms: float
    latency_max_ms: float
    decisions: int
    orders_submitted: int
    backtests: int
    trials_logged: int
    trials_inboxed: int
    models_used: list[str]
    refusals: dict[str, int]


class BudgetResult(ToolOutput):
    status: str
    freeze: dict[str, Any] | None
    limits: BudgetLimits
    usage: UsageTotals
    remaining: dict[str, float]
    on_breach: dict[str, str]


class UsageResult(ToolOutput):
    model: str
    cost_usd: float = Field(description="Cost of this report in USD from the gateway price table.")
    usage: UsageTotals
    remaining: dict[str, float]
