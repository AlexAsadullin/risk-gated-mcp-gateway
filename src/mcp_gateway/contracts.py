"""Wire models for the platform contracts this gateway consumes (conventions section 6, v1).

Inbound models are tolerant readers (``extra="allow"``): a sibling service may add optional fields without
breaking us, and those fields are passed through to the agent unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1"] = "1"

ActorKind = Literal["human", "agent", "robot"]
Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
TimeInForce = Literal["GTC", "IOC", "FOK", "POST_ONLY"]
OrderStatus = Literal["accepted", "rejected", "partially_filled", "filled", "cancelled"]
Fidelity = Literal["vectorised", "event", "tick", "reactive_sim"]
Severity = Literal["info", "warn", "block"]


class WireModel(BaseModel):
    """Base for every contract model: carries ``schema_version`` and passes unknown fields through."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: Literal["1"] = SCHEMA_VERSION


class Actor(BaseModel):
    kind: ActorKind
    id: str


# --- 6.1 Research Ledger (S6) -------------------------------------------------------------------------------


class Returns(BaseModel):
    ts: list[str] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)


class TrialRecord(WireModel):
    trial_id: str
    family: str
    strategy_id: str
    source: str
    actor: Actor
    params: dict[str, Any]
    dataset_fingerprint: str | None = None
    code_version: str | None = None
    seed: int | None = None
    started_at: str
    finished_at: str
    periods_per_year: float
    returns: Returns | None = None
    returns_path: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class TrialAck(WireModel):
    trial_id: str


class Verdict(WireModel):
    strategy_id: str
    n_trials: int
    effective_trials: float
    sharpe: float | None = None
    psr: float | None = None
    dsr: float | None = None
    pbo: float | None = None
    min_trl: float | None = None
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    leakage_flags: list[Any] = Field(default_factory=list)
    computed_at: str


class LeakageEvent(WireModel):
    source: Literal["i2", "s7", "i9"]
    strategy_id: str | None = None
    trial_id: str | None = None
    severity: Severity
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: str


# --- 6.3 Point-in-time Store (I2) ---------------------------------------------------------------------------


FilterScalar = str | int | float | bool
FilterOp = Literal["==", "!=", "<", "<=", ">", ">=", "in"]


class PitFilter(BaseModel):
    """A typed I2 predicate on one column; ``in`` takes a list, every other operator a scalar."""

    model_config = ConfigDict(extra="forbid")

    column: str
    op: FilterOp
    value: FilterScalar | list[FilterScalar]

    @classmethod
    def eq(cls, column: str, value: FilterScalar) -> PitFilter:
        return cls(column=column, op="==", value=value)

    @classmethod
    def isin(cls, column: str, values: list[FilterScalar]) -> PitFilter:
        return cls(column=column, op="in", value=values)


class PitQuery(WireModel):
    """Body of I2 ``POST /v1/query``. ``format``, ``strategy_id`` and ``trial_id`` are I2's optional fields."""

    dataset: str
    start: str
    end: str
    known_at: str
    columns: list[str] | None = None
    filters: list[PitFilter] | None = None
    mode: Literal["research", "live"]
    format: Literal["json"] = "json"
    strategy_id: str | None = None
    trial_id: str | None = None


class PitTrainingSetRequest(WireModel):
    """Body of I2 ``POST /v1/features/training-set`` with a grid spine (entities x [start, end] every step)."""

    names: list[str]
    known_at: str
    mode: Literal["research", "live"]
    entities: list[dict[str, str]]
    start: str
    end: str
    step_seconds: int
    format: Literal["json"] = "json"
    strategy_id: str | None = None


class PitResponse(WireModel):
    """JSON answer of I2; timestamp columns (valid_time, knowledge_time, ts_*) are ISO-8601 strings."""

    fingerprint: str
    columns: list[str]
    rows: list[dict[str, Any]]
    flags: list[dict[str, Any]] = Field(default_factory=list)


class PitResult(BaseModel):
    """Decoded I2 answer plus the fingerprint and any leakage flags (``X-Leakage-Flag``)."""

    rows: list[dict[str, Any]]
    fingerprint: str | None
    leakage_flags: list[str] = Field(default_factory=list)


# --- 6.4 Order API (I6 / I7) --------------------------------------------------------------------------------


class OrderRequest(WireModel):
    client_order_id: str
    account_id: str
    venue: str
    symbol: str
    side: Side
    type: OrderType
    qty: float
    price: float | None = None
    time_in_force: TimeInForce
    strategy_id: str
    actor: Actor


class Order(WireModel):
    order_id: str
    status: OrderStatus
    reject_reason: str | None = None
    client_order_id: str | None = None
    venue: str | None = None
    symbol: str | None = None
    side: Side | None = None
    qty: float | None = None
    price: float | None = None


class Position(WireModel):
    venue: str
    symbol: str
    qty: float
    avg_price: float | None = None
    mark_price: float | None = None
    notional: float | None = None


class Account(WireModel):
    account_id: str
    equity: float | None = None
    balance: float | None = None
    currency: str | None = None


# --- 6.5 Research services ----------------------------------------------------------------------------------


class ForecastRequest(WireModel):
    model: str
    symbols: list[str]
    horizon: int
    known_at: str
    quantiles: list[float]


class SymbolForecast(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    horizon: int
    mean: float
    quantiles: dict[str, float]


class ForecastResponse(WireModel):
    forecasts: list[SymbolForecast]
    model_cutoff: str | None = None


class BacktestRequest(WireModel):
    strategy: str
    params: dict[str, Any]
    universe: list[str]
    start: str
    end: str
    fidelity: Fidelity
    known_at: str


class BacktestResponse(WireModel):
    run_id: str
    trial_id: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    fidelity_report: dict[str, Any] | None = None


class ProbeRequest(WireModel):
    model_id: str
    entities: list[str]
    start: str
    end: str
    languages: list[str]


class LeakageCell(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity: str
    date: str
    memorisation_score: float
    n_probes: int


class LeakageReport(WireModel):
    model_id: str
    window: Any
    cells: list[LeakageCell] = Field(default_factory=list)
    flagged: bool
    threshold: float | None = None
