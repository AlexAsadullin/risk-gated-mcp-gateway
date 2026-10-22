"""In-process fakes of I2, S1, I5, S6, S7 and I6 that speak the platform contracts.

They exist so the gateway runs, tests and demos fully offline. Each fake is a FastAPI app; ``FakePlatform``
builds them all and hands out ``httpx.ASGITransport`` objects for :func:`mcp_gateway.app.build_gateway`.
The fakes enforce the same rules as the real services where the gateway depends on them (I2's leakage guard,
I6's account bookkeeping), and they record what they received so tests can assert on it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from mcp_gateway.contracts import (
    Account,
    BacktestRequest,
    BacktestResponse,
    ForecastRequest,
    ForecastResponse,
    LeakageCell,
    LeakageEvent,
    LeakageReport,
    Order,
    OrderRequest,
    PitFilter,
    PitQuery,
    PitTrainingSetRequest,
    Position,
    ProbeRequest,
    SymbolForecast,
    TrialAck,
    TrialRecord,
    Verdict,
)
from mcp_gateway.fakes.world import SyntheticWorld
from mcp_gateway.settings import FakeSettings
from mcp_gateway.timeutil import NS_PER_SECOND, Clock, ns_to_iso, parse_iso, to_iso, to_ns

FEATURE_LAG_BARS = 60


def _stable_unit(*parts: object) -> float:
    """A deterministic number in [0, 1) derived from the inputs (SHA-256 based)."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class DownTransport(httpx.AsyncBaseTransport):
    """A transport that always fails to connect, to simulate a service that is down."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service is down (fake)", request=request)


# --------------------------------------------------------------------------------------------- I2


def _filter_value(filters: list[PitFilter], column: str) -> Any:
    return next((f.value for f in filters if f.column == column), None)


def _leak_event(dataset: str, end: str, known_at: str) -> JSONResponse:
    event = LeakageEvent(
        source="i2",
        severity="block",
        kind="end_after_known_at",
        detail={"dataset": dataset, "end": end, "known_at": known_at},
        observed_at=known_at,
    )
    return JSONResponse(status_code=409, content=event.model_dump(mode="json"))


def _answer(request_json: str, columns: list[str], rows: list[dict[str, Any]]) -> JSONResponse:
    """I2's JSON answer: ``{schema_version, fingerprint, columns, rows, flags}`` plus the fingerprint header."""
    fingerprint = "sha256:" + hashlib.sha256(request_json.encode()).hexdigest()
    body = {"schema_version": "1", "fingerprint": fingerprint, "columns": columns, "rows": rows, "flags": []}
    return JSONResponse(content=body, headers={"X-Dataset-Fingerprint": fingerprint})


TAPE_COLUMNS = [
    "valid_time",
    "venue",
    "symbol",
    "trade_id",
    "knowledge_time",
    "ts_exchange",
    "ts_local",
    "price",
    "size",
    "side",
]


def build_fake_pit(world: SyntheticWorld, received: list[PitQuery], *, leak_future_rows: bool = False) -> FastAPI:
    """Fake of I2's shipped API: tape queries and grid training sets, JSON with ISO-8601 timestamps.

    Like the real store it orders tape rows by key columns (here ``trade_id``, which is not time order), so the
    gateway's own sort by valid time is exercised.
    """
    app = FastAPI()

    @app.post("/v1/query")
    async def query(body: PitQuery) -> JSONResponse:
        received.append(body)
        start_ns, end_ns = to_ns(parse_iso(body.start)), to_ns(parse_iso(body.end))
        known_ns = to_ns(parse_iso(body.known_at))
        if body.mode == "research" and end_ns > known_ns:
            return _leak_event(body.dataset, body.end, body.known_at)
        filters = body.filters or []
        symbol = str(_filter_value(filters, "symbol"))
        venue = _filter_value(filters, "venue")
        rows: list[dict[str, Any]] = []
        if symbol in world.base_prices:
            for i in world.bars(start_ns, end_ns):
                if world.knowledge_ns(i) > known_ns and not leak_future_rows:
                    continue
                rows.append(_tape_row(world, venue, symbol, i))
        rows.sort(key=lambda r: str(r["trade_id"]))
        return _answer(body.model_dump_json(), TAPE_COLUMNS, rows)

    @app.post("/v1/features/training-set")
    async def training_set(body: PitTrainingSetRequest) -> JSONResponse:
        start_ns, end_ns = to_ns(parse_iso(body.start)), to_ns(parse_iso(body.end))
        known_ns = to_ns(parse_iso(body.known_at))
        if body.mode == "research" and end_ns > known_ns:
            return _leak_event("features", body.end, body.known_at)
        step = body.step_seconds * NS_PER_SECOND
        rows: list[dict[str, Any]] = []
        for entity in body.entities:
            symbol = next(iter(entity.values()))
            for ts in range(start_ns, end_ns + 1, step):
                index = world.bar_index(ts)
                row: dict[str, Any] = {"ts": ns_to_iso(ts), **entity}
                for name in body.names:
                    row[name] = _feature(world, symbol, index, name) if symbol in world.base_prices else None
                rows.append(row)
        return _answer(
            body.model_dump_json(), ["ts", *sorted({k for e in body.entities for k in e}), *body.names], rows
        )

    return app


def _tape_row(world: SyntheticWorld, venue: Any, symbol: str, i: int) -> dict[str, Any]:
    side = "buy" if _stable_unit(world.seed, symbol, i, "side") < 0.5 else "sell"
    valid, known = ns_to_iso(world.valid_ns(i)), ns_to_iso(world.knowledge_ns(i))
    return {
        "valid_time": valid,
        "venue": venue,
        "symbol": symbol,
        "trade_id": f"{_stable_unit(world.seed, symbol, i, 'id'):.12f}",
        "knowledge_time": known,
        "ts_exchange": valid,
        "ts_local": known,
        "price": world.price_at_index(symbol, i),
        "size": round(0.01 + _stable_unit(world.seed, symbol, i, "size"), 4),
        "side": side,
    }


def _feature(world: SyntheticWorld, symbol: str, index: int, name: str) -> float:
    price = world.price_at_index(symbol, index)
    if name == "close":
        return price
    if name == "ret_1h":
        return price / world.price_at_index(symbol, index - FEATURE_LAG_BARS) - 1.0
    return _stable_unit(world.seed, symbol, index, name)


# --------------------------------------------------------------------------------------------- S1, I5


def build_fake_forecast(world: SyntheticWorld) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/forecast")
    async def forecast(body: ForecastRequest) -> ForecastResponse:
        out = []
        for symbol in body.symbols:
            mean = (_stable_unit(world.seed, body.model, symbol, body.known_at) - 0.5) * 0.01
            spread = 0.005 * body.horizon**0.5
            quantiles = {f"{q:g}": mean + (q - 0.5) * 2 * spread for q in body.quantiles}
            out.append(SymbolForecast(symbol=symbol, horizon=body.horizon, mean=mean, quantiles=quantiles))
        return ForecastResponse(forecasts=out, model_cutoff=body.known_at)

    return app


def build_fake_backtest(world: SyntheticWorld, received: list[BacktestRequest]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/backtests")
    async def backtest(body: BacktestRequest) -> BacktestResponse:
        received.append(body)
        key = json.dumps(body.model_dump(mode="json"), sort_keys=True)
        sharpe = round((_stable_unit(world.seed, key) - 0.3) * 3.0, 4)
        return BacktestResponse(
            run_id=f"run-{len(received):04d}",
            trial_id=str(uuid.UUID(int=int(_stable_unit(world.seed, key, "trial") * 2**64))),
            metrics={"sharpe": sharpe, "n_periods": 24 * 30},
            fidelity_report=None,
        )

    return app


# --------------------------------------------------------------------------------------------- S6, S7


@dataclass
class LedgerState:
    trials: list[TrialRecord] = field(default_factory=list)
    leakage: list[LeakageEvent] = field(default_factory=list)


def build_fake_ledger(state: LedgerState, cfg: FakeSettings, clock: Clock) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/trials")
    async def post_trial(body: TrialRecord) -> TrialAck:
        state.trials.append(body)
        return TrialAck(trial_id=body.trial_id)

    @app.post("/v1/leakage")
    async def post_leakage(body: LeakageEvent) -> dict[str, str]:
        state.leakage.append(body)
        return {"schema_version": "1", "status": "recorded"}

    @app.get("/v1/strategies/{strategy_id:path}/verdict")
    async def verdict(strategy_id: str) -> Verdict:
        trials = [t for t in state.trials if t.strategy_id == strategy_id]
        if not trials:
            raise HTTPException(status_code=404, detail="no trials for strategy")
        sharpes = [float(t.metrics["sharpe"]) for t in trials if "sharpe" in t.metrics]
        best = max(sharpes) if sharpes else None
        passed = best is not None and best >= cfg.verdict_min_sharpe and len(trials) <= cfg.verdict_max_trials
        reasons = [] if passed else ["fake verdict: best Sharpe below threshold or too many trials"]
        return Verdict(
            strategy_id=strategy_id,
            n_trials=len(trials),
            effective_trials=float(len(trials)),
            sharpe=best,
            passed=passed,
            reasons=reasons,
            thresholds={"min_sharpe": cfg.verdict_min_sharpe, "max_trials": cfg.verdict_max_trials},
            computed_at=to_iso(clock.now()),
        )

    return app


def build_fake_leakage(cfg: FakeSettings) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/probe")
    async def probe(body: ProbeRequest) -> LeakageReport:
        flagged = body.model_id in cfg.flagged_models
        score = 0.9 if flagged else 0.05
        cells = [
            LeakageCell(entity=e, date=body.start[:10], memorisation_score=score, n_probes=8) for e in body.entities
        ]
        return LeakageReport(
            model_id=body.model_id,
            window={"start": body.start, "end": body.end},
            cells=cells,
            flagged=flagged,
            threshold=0.5,
        )

    return app


# --------------------------------------------------------------------------------------------- I6


@dataclass
class ExchangeState:
    cash: dict[str, float] = field(default_factory=dict)
    positions: dict[tuple[str, str, str], float] = field(default_factory=dict)
    avg_price: dict[tuple[str, str, str], float] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    received: list[OrderRequest] = field(default_factory=list)
    mark_shock: dict[str, float] = field(default_factory=dict)


def build_fake_exchange(world: SyntheticWorld, state: ExchangeState, cfg: FakeSettings, clock: Clock) -> FastAPI:
    """Shadow exchange fake: market orders fill at the synthetic price now; limit orders rest unless marketable."""
    app = FastAPI()

    def mark(symbol: str) -> float:
        return world.price(symbol, clock.now()) * state.mark_shock.get(symbol, 1.0)

    def fill(req: OrderRequest, px: float) -> None:
        key = (req.account_id, req.venue, req.symbol)
        signed = req.qty if req.side == "buy" else -req.qty
        prev = state.positions.get(key, 0.0)
        new = prev + signed
        if prev == 0 or (prev > 0) == (signed > 0):
            state.avg_price[key] = (abs(prev) * state.avg_price.get(key, px) + abs(signed) * px) / abs(new)
        state.positions[key] = new
        state.cash[req.account_id] = state.cash.get(req.account_id, cfg.initial_cash) - signed * px

    @app.post("/v1/orders")
    async def place(body: OrderRequest) -> Order:
        state.received.append(body)
        order_id = f"ord-{len(state.received):05d}"
        if body.symbol not in world.base_prices:
            order = Order(order_id=order_id, status="rejected", reject_reason="unknown symbol")
        else:
            px = mark(body.symbol)
            marketable = body.type == "market" or (
                body.price is not None and ((body.side == "buy") == (body.price >= px))
            )
            if marketable:
                fill(body, px if body.type == "market" else float(body.price or px))
            order = Order(
                order_id=order_id,
                status="filled" if marketable else "accepted",
                client_order_id=body.client_order_id,
                venue=body.venue,
                symbol=body.symbol,
                side=body.side,
                qty=body.qty,
                price=px if body.type == "market" else body.price,
            )
        record = order.model_dump(mode="json") | {"account_id": body.account_id}
        state.orders[order_id] = record
        return Order.model_validate(record)

    @app.get("/v1/orders/{order_id}")
    async def get_order(order_id: str) -> Order:
        if order_id not in state.orders:
            raise HTTPException(status_code=404, detail="unknown order")
        return Order.model_validate(state.orders[order_id])

    @app.delete("/v1/orders/{order_id}")
    async def cancel(order_id: str) -> Order:
        if order_id not in state.orders:
            raise HTTPException(status_code=404, detail="unknown order")
        record = state.orders[order_id]
        if record["status"] in ("accepted", "partially_filled"):
            record["status"] = "cancelled"
        return Order.model_validate(record)

    def positions_of(account_id: str) -> list[Position]:
        out = []
        for (acct, venue, symbol), qty in state.positions.items():
            if acct == account_id and qty != 0:
                px = mark(symbol)
                key = (acct, venue, symbol)
                out.append(
                    Position(
                        venue=venue,
                        symbol=symbol,
                        qty=qty,
                        avg_price=state.avg_price.get(key),
                        mark_price=px,
                        notional=qty * px,
                    )
                )
        return out

    @app.get("/v1/positions")
    async def positions(account_id: str) -> dict[str, Any]:
        return {"schema_version": "1", "positions": [p.model_dump(mode="json") for p in positions_of(account_id)]}

    @app.get("/v1/accounts/{account_id}")
    async def account(account_id: str) -> Account:
        cash = state.cash.get(account_id, cfg.initial_cash)
        equity = cash + sum(p.qty * (p.mark_price or 0.0) for p in positions_of(account_id))
        return Account(account_id=account_id, equity=equity, balance=cash, currency=cfg.currency)

    return app


# --------------------------------------------------------------------------------------------- bundle


@dataclass
class FakePlatform:
    """All fakes plus the state they record. ``down`` names services that refuse connections."""

    world: SyntheticWorld
    cfg: FakeSettings
    clock: Clock
    down: set[str] = field(default_factory=set)
    leak_future_rows: bool = False
    pit_queries: list[PitQuery] = field(default_factory=list)
    backtests: list[BacktestRequest] = field(default_factory=list)
    ledger: LedgerState = field(default_factory=LedgerState)
    exchange: ExchangeState = field(default_factory=ExchangeState)
    execution: ExchangeState = field(default_factory=ExchangeState)

    def apps(self) -> dict[str, FastAPI]:
        return {
            "pit": build_fake_pit(self.world, self.pit_queries, leak_future_rows=self.leak_future_rows),
            "forecast": build_fake_forecast(self.world),
            "backtest": build_fake_backtest(self.world, self.backtests),
            "ledger": build_fake_ledger(self.ledger, self.cfg, self.clock),
            "leakage": build_fake_leakage(self.cfg),
            "shadow": build_fake_exchange(self.world, self.exchange, self.cfg, self.clock),
            "execution": build_fake_exchange(self.world, self.execution, self.cfg, self.clock),
        }

    def transports(self) -> dict[str, httpx.AsyncBaseTransport]:
        out: dict[str, httpx.AsyncBaseTransport] = {}
        for name, app in self.apps().items():
            out[name] = DownTransport() if name in self.down else httpx.ASGITransport(app=app)
        return out
