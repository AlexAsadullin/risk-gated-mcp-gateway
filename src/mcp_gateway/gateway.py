"""The gateway core: sessions, the contract-6.6 tools, time travel, budgets, trial capture and pre-trade risk.

Transport-agnostic: the MCP layer (``mcp_server``) and the admin API (``admin``) are thin adapters over this
class. Every tool call goes through :meth:`Gateway.invoke`, which authenticates, serialises calls per session,
enforces the frozen state, turns failures into structured refusals and writes one audit entry per decision.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from mcp_gateway import budgets as bud
from mcp_gateway.admin_models import CreateSessionRequest, CreateSessionResponse, SessionView
from mcp_gateway.audit import AuditLog, Decision
from mcp_gateway.brokers.base import BrokerAdapter, BrokerUnavailable, OrderNotFound
from mcp_gateway.brokers.router import BrokerDisabled, BrokerRouter, UnknownBroker
from mcp_gateway.clients.base import DownstreamError, DownstreamUnavailable
from mcp_gateway.clients.pit import PitLeakage
from mcp_gateway.contracts import (
    Actor,
    BacktestRequest,
    BacktestResponse,
    Fidelity,
    ForecastRequest,
    LeakageEvent,
    LeakageReport,
    Order,
    OrderRequest,
    OrderType,
    PitFilter,
    PitQuery,
    PitResult,
    PitTrainingSetRequest,
    Position,
    ProbeRequest,
    Severity,
    Side,
)
from mcp_gateway.downstream import Downstream
from mcp_gateway.outputs import (
    AccountResult,
    BacktestResult,
    BudgetResult,
    CancelResult,
    ClockResult,
    FeaturesResult,
    ForecastPoint,
    ForecastResult,
    InstrumentResult,
    MarketDataResult,
    MarketSliceResult,
    OrderResult,
    PositionsResult,
    UsageResult,
    UsageTotals,
    VerdictResult,
)
from mcp_gateway.refusals import Refusal, RefusalBody, RefusalCode
from mcp_gateway.risk import (
    CheckResult,
    OrderIntent,
    failed,
    gross_exposure_after,
    instrument_checks,
    order_notional,
    pre_trade_checks,
)
from mcp_gateway.sessions import (
    FreezeInfo,
    Session,
    SessionStatus,
    SessionStore,
    generate_api_key,
    hash_api_key,
    keys_equal,
)
from mcp_gateway.settings import BreachAction, Mode, Settings, UnknownModelPolicy
from mcp_gateway.timetravel import Window, drop_future_rows, guard_window, sort_by_valid_time
from mcp_gateway.timeutil import Clock, from_ns, parse_iso, to_iso, to_ns
from mcp_gateway.trials import Destination, LoggedTrial, TrialLogger, build_trial, ledger_strategy_id

log = structlog.get_logger(__name__)

HTTP_NOT_FOUND = 404
SEED_PARAM = "seed"
LEAKAGE_SOURCE: Literal["i9"] = "i9"  # contract 6.1 extension accepted by S6
LOOKAHEAD_KIND = "end_after_known_at"
LOOKAHEAD_CLAMPED_KIND = "end_clamped_to_known_at"
FEATURES_DATASET = "features.training_set"
ADMIN_ACTOR = "admin"
LeakageStatus = Literal["not_required", "clean", "flagged", "not_audited"]
ToolOutcome = BaseModel


@dataclass(frozen=True)
class Fetched:
    """Rows from I2 after the gateway's own future-row filter, ordered by valid time."""

    rows: list[dict[str, Any]]
    fingerprint: str | None
    dropped: int
    flags: list[str]


@dataclass(frozen=True)
class Credentials:
    """What a transport knows about the caller: an API key and, for admin callers, a session to act in."""

    api_key: str | None
    session_override: str | None = None


class AdminRejection(Exception):
    """An admin request the gateway will not carry out; mapped to an HTTP error by the admin API."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _summarise(output: BaseModel) -> dict[str, Any]:
    """A compact audit outcome: large row payloads are replaced by their length."""
    data = output.model_dump(mode="json")
    if isinstance(data.get("rows"), list):
        data["rows"] = f"<{len(data['rows'])} rows>"
    return data


FILL_METRICS = ("filled_qty", "avg_price", "fee")


def fill_metrics(order: Order) -> dict[str, Any]:
    """Fill details the adapter reported (mirror: filled quantity, average price, commission in roubles)."""
    extra = order.model_extra or {}
    return {k: extra[k] for k in FILL_METRICS if isinstance(extra.get(k), int | float)}


def _entity(symbol: str, separator: str) -> str:
    return symbol.split(separator, 1)[0]


@contextmanager
def refusing_downstream() -> Iterator[None]:
    """Turn downstream transport failures into structured refusals."""
    try:
        yield
    except DownstreamUnavailable as exc:
        raise Refusal(
            RefusalCode.DOWNSTREAM_UNAVAILABLE,
            f"{exc.service} is unavailable; retry later",
            details={"service": exc.service, "reason": exc.reason},
        ) from exc
    except OrderNotFound as exc:
        raise Refusal(RefusalCode.NOT_FOUND, f"no order {exc.order_id} at this session's broker") from exc
    except BrokerUnavailable as exc:
        raise Refusal(
            RefusalCode.DOWNSTREAM_UNAVAILABLE,
            f"broker {exc.broker} is unavailable: {exc.reason}",
            details={"service": exc.broker, "reason": exc.reason},
        ) from exc
    except DownstreamError as exc:
        code = RefusalCode.NOT_FOUND if exc.status_code == HTTP_NOT_FOUND else RefusalCode.DOWNSTREAM_ERROR
        raise Refusal(
            code,
            f"{exc.service} rejected the request (HTTP {exc.status_code})",
            details={"service": exc.service, "status_code": exc.status_code, "body": exc.body},
        ) from exc


class Gateway:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        audit: AuditLog,
        downstream: Downstream,
        trials: TrialLogger,
        clock: Clock,
        brokers: BrokerRouter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.audit = audit
        self.downstream = downstream
        self.trials = trials
        self.clock = clock
        self.brokers = brokers
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ admin

    def is_admin_key(self, key: str | None) -> bool:
        expected = self.settings.admin_api_key
        if key is None or expected is None:
            return False
        return keys_equal(key, expected.get_secret_value())

    def _check_clock_value(self, known_at: datetime, current_ns: int | None) -> None:
        if not self.settings.clock.allow_future and known_at > self.clock.now():
            raise AdminRejection(422, "invalid_clock", "known_at is in the future of the wall clock")
        if current_ns is not None and not self.settings.clock.allow_rewind and to_ns(known_at) < current_ns:
            raise AdminRejection(409, "clock_rewind", "the session clock can only move forward")

    def create_session(self, request: CreateSessionRequest) -> CreateSessionResponse:
        if request.mode is Mode.LIVE and not self.settings.modes.live_enabled:
            raise AdminRejection(403, RefusalCode.LIVE_DISABLED, "live mode is disabled by configuration")
        known_at_ns: int | None = None
        if request.known_at is not None:
            try:
                known_at = parse_iso(request.known_at)
            except ValueError as exc:
                raise AdminRejection(422, "invalid_clock", f"known_at is not ISO-8601: {exc}") from exc
            self._check_clock_value(known_at, None)
            known_at_ns = to_ns(known_at)
        elif request.mode is Mode.RESEARCH:
            raise AdminRejection(422, "invalid_clock", "research sessions need known_at")
        try:
            broker = self.brokers.check_choice(request.mode, request.broker)
        except BrokerDisabled as exc:
            raise AdminRejection(403, "broker_disabled", str(exc)) from exc
        except UnknownBroker as exc:
            raise AdminRejection(422, "invalid_broker", str(exc)) from exc
        api_key = generate_api_key(self.settings.auth.api_key_bytes)
        session = Session(
            session_id=str(uuid.uuid4()),
            agent_id=request.agent_id,
            mode=request.mode,
            known_at_ns=known_at_ns,
            account_id=request.account_id or self.settings.orders.account_id_template.format(agent_id=request.agent_id),
            limits=request.budgets.apply(self.settings.budgets.defaults),
            created_at_ns=to_ns(self.clock.now()),
            api_key_hash=hash_api_key(api_key),
            broker=broker,
        )
        self.store.create(session)
        self._audit_admin("create_session", session, request.model_dump(mode="json", exclude_none=True))
        view = SessionView.of(session)
        return CreateSessionResponse(**view.model_dump(), api_key=api_key, mcp_path=self.settings.server.mcp_path)

    def _require(self, session_id: str) -> Session:
        session = self.store.get(session_id)
        if session is None:
            raise AdminRejection(404, RefusalCode.NOT_FOUND, f"no session {session_id}")
        return session

    def get_session(self, session_id: str) -> SessionView:
        return SessionView.of(self._require(session_id))

    def list_sessions(self, agent_id: str | None) -> list[SessionView]:
        return [SessionView.of(s) for s in self.store.list(agent_id)]

    async def set_clock(self, session_id: str, known_at_text: str) -> SessionView:
        async with self._locks[session_id]:
            session = self._require(session_id)
            try:
                known_at = parse_iso(known_at_text)
            except ValueError as exc:
                raise AdminRejection(422, "invalid_clock", f"known_at is not ISO-8601: {exc}") from exc
            self._check_clock_value(known_at, session.known_at_ns)
            session.known_at_ns = to_ns(known_at)
            self.store.save(session)
            self._audit_admin("set_clock", session, {"known_at": to_iso(known_at)})
            return SessionView.of(session)

    async def set_status(self, session_id: str, status: SessionStatus) -> SessionView:
        async with self._locks[session_id]:
            session = self._require(session_id)
            if session.status is SessionStatus.CLOSED:
                raise AdminRejection(409, RefusalCode.SESSION_CLOSED, "session is closed")
            session.status = status
            if status is SessionStatus.ACTIVE:
                session.freeze = None
            self.store.save(session)
            self._audit_admin(f"set_status:{status}", session, {})
            return SessionView.of(session)

    def _audit_admin(self, action: str, session: Session, args: dict[str, Any]) -> None:
        self.audit.append(
            ts=self.clock.now(),
            actor=ADMIN_ACTOR,
            session_id=session.session_id,
            mode=session.mode,
            action=action,
            decision=Decision.ADMIN,
            args=args,
        )

    # ------------------------------------------------------------------ dispatch

    def authenticate(self, credentials: Credentials) -> Session:
        key = credentials.api_key
        if key is None:
            raise Refusal(RefusalCode.UNAUTHENTICATED, "missing API key (Authorization: Bearer <key>)")
        if credentials.session_override is not None and self.is_admin_key(key):
            session = self.store.get(credentials.session_override)
        else:
            session = self.store.get_by_key_hash(hash_api_key(key))
        if session is None:
            raise Refusal(RefusalCode.UNAUTHENTICATED, "unknown API key or session")
        return session

    async def invoke(
        self,
        tool: str,
        credentials: Credentials,
        args: dict[str, Any],
        impl: Callable[[Session], Awaitable[BaseModel]],
    ) -> BaseModel | RefusalBody:
        """Run one tool call for the authenticated session and audit the decision."""
        try:
            session = self.authenticate(credentials)
        except Refusal as refusal:
            self.audit.append(
                ts=self.clock.now(),
                actor="unknown",
                session_id=None,
                mode=None,
                action=tool,
                decision=Decision.REFUSED,
                code=refusal.code,
                args=args,
            )
            return refusal.body(tool=tool, session_status=None)
        async with self._locks[session.session_id]:
            session = self.store.get(session.session_id) or session
            try:
                self._check_status(session, tool)
                output = await self._call(impl, session)
            except Refusal as refusal:
                return self._refused(session, tool, args, refusal)
            self.store.save(session)
            self.audit.append(
                ts=self.clock.now(),
                actor=session.agent_id,
                session_id=session.session_id,
                mode=session.mode,
                action=tool,
                decision=Decision.ALLOWED,
                args=args,
                outcome=_summarise(output),
            )
            return output

    async def _call(self, impl: Callable[[Session], Awaitable[BaseModel]], session: Session) -> BaseModel:
        with refusing_downstream():
            return await impl(session)

    def _check_status(self, session: Session, tool: str) -> None:
        if session.status is SessionStatus.CLOSED:
            raise Refusal(RefusalCode.SESSION_CLOSED, "session is closed")
        if session.status is SessionStatus.FROZEN and tool not in self.settings.budgets.frozen_allowed_tools:
            freeze = session.freeze.model_dump(mode="json") if session.freeze else {}
            raise Refusal(
                RefusalCode.SESSION_FROZEN,
                "session is frozen after a budget breach; only read-only tools are available",
                details={"freeze": freeze, "allowed_tools": self.settings.budgets.frozen_allowed_tools},
            )

    def _refused(self, session: Session, tool: str, args: dict[str, Any], refusal: Refusal) -> RefusalBody:
        session.usage.count_refusal(refusal.code)
        if refusal.freeze and session.status is SessionStatus.ACTIVE:
            budget = refusal.details.get("freeze_budget")
            session.status = SessionStatus.FROZEN
            session.freeze = FreezeInfo(
                code=refusal.code, budget=budget, message=refusal.message, at=to_iso(self.clock.now())
            )
            log.warning("session_frozen", session_id=session.session_id, reason=refusal.message)
        self.store.save(session)
        body = refusal.body(tool=tool, session_status=session.status)
        self.audit.append(
            ts=self.clock.now(),
            actor=session.agent_id,
            session_id=session.session_id,
            mode=session.mode,
            action=tool,
            decision=Decision.REFUSED,
            code=refusal.code,
            args=args,
            outcome=body.model_dump(mode="json"),
        )
        return body

    # ------------------------------------------------------------------ helpers

    def known_at(self, session: Session) -> datetime:
        """The session clock: operator-set ``known_at``, or the wall clock for paper/live without one."""
        if session.known_at_ns is not None:
            return from_ns(session.known_at_ns)
        return self.clock.now()

    def _pit_mode(self, session: Session) -> Literal["research", "live"]:
        return "research" if session.mode is Mode.RESEARCH else "live"

    def _strategy(self, session: Session, strategy: str) -> str:
        if not strategy.strip():
            raise Refusal(RefusalCode.INVALID_REQUEST, "strategy id must not be empty")
        return ledger_strategy_id(self.settings.ledger.strategy_id_template, session.agent_id, strategy)

    async def _guard(
        self, session: Session, start: str, end: str, *, tool: str, strategy_id: str | None = None
    ) -> Window:
        """Check a window against the session clock; report every look-ahead attempt to S6 (source ``i9``)."""
        try:
            window = guard_window(
                start,
                end,
                known_at=self.known_at(session),
                policy=self.settings.data.time_travel_policy,
                max_window_days=self.settings.data.max_window_days,
            )
        except Refusal as refusal:
            if refusal.code is RefusalCode.TIME_TRAVEL:
                await self._report_lookahead(session, tool, strategy_id, "block", refusal.details)
            raise
        if window.clamped:
            details = {"requested_start": start, "requested_end": end, "known_at": to_iso(window.end)}
            await self._report_lookahead(session, tool, strategy_id, "warn", details)
        return window

    async def _report_lookahead(
        self, session: Session, tool: str, strategy_id: str | None, severity: Severity, details: dict[str, Any]
    ) -> None:
        event = LeakageEvent(
            source=LEAKAGE_SOURCE,
            strategy_id=strategy_id,
            severity=severity,
            kind=LOOKAHEAD_KIND if severity == "block" else LOOKAHEAD_CLAMPED_KIND,
            detail={"tool": tool, "agent_id": session.agent_id, "session_id": session.session_id, **details},
            observed_at=to_iso(self.clock.now()),
        )
        await self.trials.report_leakage(event)

    async def _query(
        self,
        session: Session,
        *,
        dataset: str,
        window: Window,
        columns: list[str] | None,
        filters: list[PitFilter],
        touch: bool = True,
    ) -> Fetched:
        known_at = self.known_at(session)
        query = PitQuery(
            dataset=dataset,
            start=to_iso(window.start),
            end=to_iso(window.end),
            known_at=to_iso(known_at),
            columns=columns,
            filters=filters,
            mode=self._pit_mode(session),
        )
        return await self._fetch(session, self.downstream.pit.query(query), window, touch=touch)

    async def _fetch(self, session: Session, call: Awaitable[PitResult], window: Window, *, touch: bool) -> Fetched:
        try:
            result = await call
        except PitLeakage as exc:
            raise Refusal(
                RefusalCode.TIME_TRAVEL,
                "the point-in-time store refused the query as a leak",
                details={"enforced_by": "i2", "leakage_event": exc.event},
            ) from exc
        rows, dropped = drop_future_rows(result.rows, to_ns(self.known_at(session)))
        if touch:
            session.usage.touch_window(to_ns(window.start), to_ns(window.end))
        return Fetched(sort_by_valid_time(rows), result.fingerprint, dropped, result.leakage_flags)

    def _truncate(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        limit = self.settings.data.max_rows
        if len(rows) <= limit:
            return rows, False
        return rows[-limit:], True

    def _totals(self, session: Session) -> UsageTotals:
        u = session.usage
        return UsageTotals(
            tokens_in=u.tokens_in,
            tokens_out=u.tokens_out,
            token_cost_usd=u.token_cost_usd,
            latency_total_ms=u.latency_total_ms,
            latency_max_ms=u.latency_max_ms,
            decisions=u.decisions,
            orders_submitted=u.orders_submitted,
            backtests=u.backtests,
            trials_logged=u.trials_logged,
            trials_inboxed=u.trials_inboxed,
            models_used=list(u.models_used),
            refusals=dict(u.refusals),
        )

    def _remaining(self, session: Session) -> dict[str, float]:
        return bud.remaining(session.limits, session.usage, to_ns(self.known_at(session)))

    def _as_of(self, session: Session) -> str | None:
        """The session clock for I6/I7 when the operator drives one; ``None`` means the service's own clock."""
        return None if session.known_at_ns is None else to_iso(self.known_at(session))

    def _breach_refusal(self, breaches: list[bud.Breach], **details: Any) -> Refusal:
        freezes = bud.breach_freezes(breaches, self.settings.budgets.on_breach)
        freeze_budget = next(
            (b.kind for b in breaches if self.settings.budgets.on_breach[b.kind] is BreachAction.FREEZE), None
        )
        return Refusal(
            RefusalCode.BUDGET_BREACHED,
            "; ".join(b.message for b in breaches),
            details={
                "breaches": [b.model_dump(mode="json") for b in breaches],
                "freezes_session": freezes,
                "freeze_budget": freeze_budget,
                **details,
            },
            freeze=freezes,
        )

    async def _log_trial(self, session: Session, record_kwargs: dict[str, Any]) -> LoggedTrial:
        record = build_trial(settings=self.settings.ledger, agent_id=session.agent_id, **record_kwargs)
        logged = await self.trials.log(record)
        if logged.destination is Destination.LEDGER:
            session.usage.trials_logged += 1
        else:
            session.usage.trials_inboxed += 1
        return logged

    def _broker(self, session: Session) -> BrokerAdapter:
        """Contract 6.8: the session's execution adapter (its own choice, else the mode's default)."""
        try:
            return self.brokers.get(session.broker or self.brokers.default_for(session.mode))
        except BrokerDisabled as exc:
            # A session stored before the adapter was switched off: refuse instead of reaching the broker.
            raise Refusal(RefusalCode.MODE_FORBIDDEN, str(exc), details={"broker": session.broker}) from exc

    def _require_trading(self, session: Session) -> None:
        if session.mode is Mode.RESEARCH:
            raise Refusal(
                RefusalCode.MODE_FORBIDDEN,
                "research sessions cannot trade or hold positions; use run_backtest, or ask for a paper session",
            )

    # ------------------------------------------------------------------ tools

    async def clock_tool(self, credentials: Credentials) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> ClockResult:
            return ClockResult(
                session_id=session.session_id,
                agent_id=session.agent_id,
                mode=session.mode,
                known_at=to_iso(self.known_at(session)),
                known_at_source="session" if session.known_at_ns is not None else "wall",
                status=session.status,
            )

        return await self.invoke("clock", credentials, {}, impl)

    async def market_data(
        self, credentials: Credentials, *, venue: str, symbol: str, channel: str, start: str, end: str
    ) -> BaseModel | RefusalBody:
        args = {"venue": venue, "symbol": symbol, "channel": channel, "start": start, "end": end}

        async def impl(session: Session) -> MarketDataResult:
            if channel not in self.settings.data.channels:
                raise Refusal(
                    RefusalCode.INVALID_REQUEST,
                    f"unknown channel {channel!r}",
                    details={"channels": self.settings.data.channels},
                )
            window = await self._guard(session, start, end, tool="market_data")
            dataset = self.settings.data.market_data_dataset_template.format(channel=channel)
            fetched = await self._query(
                session,
                dataset=dataset,
                window=window,
                columns=None,
                filters=[PitFilter.eq("venue", venue), PitFilter.eq("symbol", symbol)],
            )
            session.usage.touch_symbols([symbol])
            return MarketDataResult(
                venue=venue, symbol=symbol, channel=channel, **self._data_fields(session, dataset, window, fetched)
            )

        return await self.invoke("market_data", credentials, args, impl)

    def _data_fields(self, session: Session, dataset: str, window: Window, fetched: Fetched) -> dict[str, Any]:
        kept, truncated = self._truncate(fetched.rows)
        return {
            "dataset": dataset,
            "start": to_iso(window.start),
            "end": to_iso(window.end),
            "known_at": to_iso(self.known_at(session)),
            "clamped": window.clamped,
            "fingerprint": fetched.fingerprint,
            "n_rows": len(fetched.rows),
            "truncated": truncated,
            "dropped_future_rows": fetched.dropped,
            "leakage_flags": fetched.flags,
            "rows": kept,
        }

    async def features(
        self, credentials: Credentials, *, names: list[str], symbols: list[str], start: str, end: str
    ) -> BaseModel | RefusalBody:
        args = {"names": names, "symbols": symbols, "start": start, "end": end}
        cfg = self.settings.data

        async def impl(session: Session) -> FeaturesResult:
            if not names or not symbols:
                raise Refusal(RefusalCode.INVALID_REQUEST, "names and symbols must be non-empty")
            window = await self._guard(session, start, end, tool="features")
            request = PitTrainingSetRequest(
                names=names,
                known_at=to_iso(self.known_at(session)),
                mode=self._pit_mode(session),
                entities=[{cfg.feature_entity_column: symbol} for symbol in symbols],
                start=to_iso(window.start),
                end=to_iso(window.end),
                step_seconds=cfg.feature_step_seconds,
            )
            fetched = await self._fetch(session, self.downstream.pit.training_set(request), window, touch=True)
            session.usage.touch_symbols(symbols)
            return FeaturesResult(
                names=names, symbols=symbols, **self._data_fields(session, FEATURES_DATASET, window, fetched)
            )

        return await self.invoke("features", credentials, args, impl)

    async def forecast(
        self, credentials: Credentials, *, model: str, symbols: list[str], horizon: int
    ) -> BaseModel | RefusalBody:
        args = {"model": model, "symbols": symbols, "horizon": horizon}
        cfg = self.settings.forecast

        async def impl(session: Session) -> ForecastResult:
            if not symbols or len(symbols) > cfg.max_symbols:
                raise Refusal(RefusalCode.INVALID_REQUEST, f"symbols must hold 1..{cfg.max_symbols} entries")
            if not 1 <= horizon <= cfg.max_horizon:
                raise Refusal(RefusalCode.INVALID_REQUEST, f"horizon must be in 1..{cfg.max_horizon}")
            known_at = to_iso(self.known_at(session))
            response = await self.downstream.forecast.forecast(
                ForecastRequest(
                    model=model, symbols=symbols, horizon=horizon, known_at=known_at, quantiles=cfg.quantiles
                )
            )
            session.usage.touch_symbols(symbols)
            return ForecastResult(
                model=model,
                horizon=horizon,
                known_at=known_at,
                model_cutoff=response.model_cutoff,
                forecasts=[
                    ForecastPoint(symbol=f.symbol, horizon=f.horizon, mean=f.mean, quantiles=f.quantiles)
                    for f in response.forecasts
                ],
            )

        return await self.invoke("forecast", credentials, args, impl)

    async def run_backtest(
        self,
        credentials: Credentials,
        *,
        strategy: str,
        params: dict[str, Any],
        universe: list[str],
        start: str,
        end: str,
        fidelity: Fidelity,
    ) -> BaseModel | RefusalBody:
        args = {
            "strategy": strategy,
            "params": params,
            "universe": universe,
            "start": start,
            "end": end,
            "fidelity": fidelity,
        }

        async def impl(session: Session) -> BacktestResult:
            strategy_id = self._strategy(session, strategy)
            started = self.clock.now()
            known_at = to_iso(self.known_at(session))
            idea = {"strategy": strategy, "params": params, "universe": universe, "start": start, "end": end}
            idea |= {"fidelity": fidelity, "known_at": known_at, "session_id": session.session_id}
            seed = params.get("seed") if isinstance(params.get("seed"), int) else None
            family = self.settings.ledger.backtest_family_template.format(
                strategy=strategy, universe="+".join(universe)
            )
            session.usage.backtests += 1
            session.usage.add_strategy(strategy_id)
            try:
                with refusing_downstream():
                    response = await self._backtest(
                        session, strategy, params, universe, start, end, fidelity, strategy_id=strategy_id
                    )
            except Refusal as refusal:
                await self._log_trial(
                    session,
                    {
                        "kind": "backtest",
                        "family": family,
                        "strategy_id": strategy_id,
                        "params": idea,
                        "metrics": {},
                        "tags": [f"refused:{refusal.code}"],
                        "started_at": started,
                        "finished_at": self.clock.now(),
                        "seed": seed,
                    },
                )
                raise
            if response.trial_id is not None:
                # I5 logged this run in S6 under our strategy id and agent actor; a second record would count
                # the same backtest twice in n_trials.
                logged = LoggedTrial(trial_id=response.trial_id, destination=Destination.I5)
                session.usage.trials_logged += 1
            else:
                logged = await self._log_trial(
                    session,
                    {
                        "kind": "backtest",
                        "family": family,
                        "strategy_id": strategy_id,
                        "params": idea | {"run_id": response.run_id},
                        "metrics": response.metrics,
                        "tags": ["completed"],
                        "started_at": started,
                        "finished_at": self.clock.now(),
                        "seed": seed,
                    },
                )
            return BacktestResult(
                run_id=response.run_id,
                strategy=strategy,
                ledger_strategy_id=strategy_id,
                backtest_trial_id=response.trial_id,
                gateway_trial_id=logged.trial_id,
                logged_to=logged.destination,
                known_at=known_at,
                start=start,
                end=end,
                fidelity=fidelity,
                metrics=response.metrics,
                fidelity_report=response.fidelity_report,
            )

        return await self.invoke("run_backtest", credentials, args, impl)

    async def _backtest(
        self,
        session: Session,
        strategy: str,
        params: dict[str, Any],
        universe: list[str],
        start: str,
        end: str,
        fidelity: Fidelity,
        *,
        strategy_id: str,
    ) -> BacktestResponse:
        if fidelity not in self.settings.backtest.fidelities:
            raise Refusal(
                RefusalCode.INVALID_REQUEST,
                f"fidelity {fidelity!r} not enabled",
                details={"fidelities": self.settings.backtest.fidelities},
            )
        if not universe or len(universe) > self.settings.backtest.max_universe:
            raise Refusal(
                RefusalCode.INVALID_REQUEST,
                f"universe must hold 1..{self.settings.backtest.max_universe} symbols",
            )
        window = await self._guard(session, start, end, tool="run_backtest", strategy_id=strategy_id)
        session.usage.touch_symbols(universe)
        session.usage.touch_window(to_ns(window.start), to_ns(window.end))
        return await self.downstream.backtest.run(
            BacktestRequest(
                strategy=strategy,
                # The tool contract carries the seed inside params; I5 takes it as its own field and rejects
                # unknown strategy parameters, so it moves out of params here.
                params={k: v for k, v in params.items() if k != SEED_PARAM},
                universe=universe,
                start=to_iso(window.start),
                end=to_iso(window.end),
                fidelity=fidelity,
                known_at=to_iso(self.known_at(session)),
                strategy_id=strategy_id,
                actor=Actor(kind="agent", id=session.agent_id),
                seed=params[SEED_PARAM] if isinstance(params.get(SEED_PARAM), int) else None,
            )
        )

    async def verdict(self, credentials: Credentials, *, strategy_id: str) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> VerdictResult:
            mapped = self._strategy(session, strategy_id)
            verdict = await self.downstream.ledger.verdict(mapped)
            status, reports = await self._leakage_audit(session, mapped)
            reasons = list(verdict.reasons)
            if not verdict.passed:
                reasons.append("S6 verdict did not pass")
            if status in ("flagged", "not_audited"):
                reasons.append(f"S7 leakage audit: {status}")
            return VerdictResult(
                strategy_id=strategy_id,
                ledger_strategy_id=mapped,
                verdict=verdict,
                llm_driven=bool(session.usage.models_used),
                leakage_status=status,
                leakage_reports=reports,
                promotable=verdict.passed and status in ("clean", "not_required"),
                reasons=reasons,
            )

        return await self.invoke("verdict", credentials, {"strategy_id": strategy_id}, impl)

    async def _leakage_audit(self, session: Session, strategy_id: str) -> tuple[LeakageStatus, list[LeakageReport]]:
        """Ask S7 to probe every LLM this session used, over the entities and window the session touched."""
        if not session.usage.models_used:
            return "not_required", []
        cfg = self.settings.leakage
        end = from_ns(session.usage.data_end_ns) if session.usage.data_end_ns is not None else self.known_at(session)
        start = (
            from_ns(session.usage.data_start_ns)
            if session.usage.data_start_ns is not None
            else end - timedelta(days=cfg.default_window_days)
        )
        entities = sorted({_entity(s, cfg.entity_separator) for s in session.usage.symbols_touched})
        reports: list[LeakageReport] = []
        for model in session.usage.models_used:
            request = ProbeRequest(
                model_id=model,
                entities=entities,
                start=to_iso(start),
                end=to_iso(end),
                languages=cfg.languages,
            )
            try:
                reports.append(await self.downstream.leakage.probe(request))
            except (DownstreamUnavailable, DownstreamError) as exc:
                log.info("leakage_probe_unavailable", model=model, error=str(exc))
                return "not_audited", reports
        flagged = [r for r in reports if r.flagged]
        for report in flagged:
            await self._report_flag(strategy_id, report)
        return ("flagged" if flagged else "clean"), reports

    async def _report_flag(self, strategy_id: str, report: LeakageReport) -> None:
        event = LeakageEvent(
            source="s7",
            strategy_id=strategy_id,
            trial_id=None,
            severity="block",
            kind=self.settings.leakage.flag_event_kind,
            detail={"model_id": report.model_id, "threshold": report.threshold, "window": report.window},
            observed_at=to_iso(self.clock.now()),
        )
        try:
            await self.downstream.ledger.post_leakage(event)
        except (DownstreamUnavailable, DownstreamError) as exc:
            log.info("leakage_event_not_delivered", strategy_id=strategy_id, error=str(exc))

    async def place_order(
        self,
        credentials: Credentials,
        *,
        venue: str,
        symbol: str,
        side: Side,
        type: OrderType,
        qty: float,
        price: float | None,
        strategy_id: str,
        rationale: str,
    ) -> BaseModel | RefusalBody:
        args = {
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "type": type,
            "qty": qty,
            "price": price,
            "strategy_id": strategy_id,
            "rationale": rationale,
        }
        intent = OrderIntent(venue=venue, symbol=symbol, side=side, type=type, qty=qty, price=price)

        async def impl(session: Session) -> OrderResult:
            mapped = self._strategy(session, strategy_id)
            session.usage.add_strategy(mapped)
            session.usage.touch_symbols([symbol])
            started = self.clock.now()
            trial = {"kind": "order-intent", "strategy_id": mapped, "started_at": started}
            idea = args | {"mode": session.mode, "known_at": to_iso(self.known_at(session))}
            try:
                with refusing_downstream():
                    order, routed_to, ref, checks = await self._route_order(session, intent, mapped)
            except Refusal as refusal:
                trial |= {"params": idea, "metrics": {}, "tags": [f"refused:{refusal.code}"]}
                await self._log_trial(session, trial | {"finished_at": self.clock.now()})
                raise
            trial |= {
                "params": idea | {"order_id": order.order_id, "reference_price": ref},
                "metrics": {"status": order.status} | fill_metrics(order),
                "tags": [f"routed:{routed_to}", f"status:{order.status}"],
            }
            logged = await self._log_trial(session, trial | {"finished_at": self.clock.now()})
            return OrderResult(
                order=order,
                routed_to=routed_to,
                ledger_strategy_id=mapped,
                notional=order_notional(intent, ref),
                reference_price=ref,
                checks=checks,
                gateway_trial_id=logged.trial_id,
                logged_to=logged.destination,
            )

        return await self.invoke("place_order", credentials, args, impl)

    async def _route_order(
        self, session: Session, intent: OrderIntent, strategy_id: str
    ) -> tuple[Order, str, float | None, list[CheckResult]]:
        self._require_trading(session)
        if session.mode is Mode.LIVE:
            await self._live_gate(session, strategy_id)
        broker = self._broker(session)
        as_of = self._as_of(session)
        reference = await self._reference_price(session, intent)
        positions = await broker.positions(session.account_id, known_at=as_of)
        await self._check_drawdown(session, broker)
        rules = await broker.instrument(intent.venue, intent.symbol, known_at=as_of)
        checks = pre_trade_checks(intent, reference_price=reference, positions=positions, risk=self.settings.risk)
        checks += instrument_checks(intent, rules, reference, required=self.settings.risk.require_instrument_rules)
        if failed(checks):
            raise Refusal(
                RefusalCode.PRE_TRADE_REJECTED,
                "pre-trade checks failed: " + ", ".join(c.name for c in failed(checks)),
                details={"checks": [c.model_dump(mode="json") for c in checks]},
            )
        # The rate window runs on the session clock: a replayed episode spans session time, not wall time.
        now_ns = to_ns(self.known_at(session))
        notional = order_notional(intent, reference) or 0.0
        breaches = bud.order_breaches(
            session.limits,
            session.usage,
            order_notional=notional,
            gross_exposure_after=gross_exposure_after(positions, intent, reference),
            now_ns=now_ns,
        )
        if breaches:
            raise self._breach_refusal(breaches)
        request = OrderRequest(
            client_order_id=str(uuid.uuid4()),
            account_id=session.account_id,
            venue=intent.venue,
            symbol=intent.symbol,
            side=intent.side,
            type=intent.type,
            qty=intent.qty,
            price=intent.price if intent.type == "limit" else None,
            time_in_force=self.settings.orders.default_time_in_force,
            strategy_id=strategy_id,
            actor=Actor(kind="agent", id=session.agent_id),
        )
        order = await broker.place(request, known_at=as_of)
        usage = session.usage
        usage.order_times_ns = [*bud.prune_order_times(usage.order_times_ns, now_ns, session.limits.order_window_s)]
        usage.order_times_ns.append(now_ns)
        usage.orders_submitted += 1
        return order, broker.routed_to, reference, checks

    async def _live_gate(self, session: Session, strategy_id: str) -> None:
        modes = self.settings.modes
        if not modes.live_enabled:
            raise Refusal(RefusalCode.LIVE_DISABLED, "live trading is disabled by configuration")
        reasons: list[str] = []
        if modes.live_require_verdict:
            verdict = await self.downstream.ledger.verdict(strategy_id)
            if not verdict.passed:
                reasons.append("S6 verdict has not passed")
        if modes.live_require_clean_leakage:
            status, _ = await self._leakage_audit(session, strategy_id)
            if status in ("flagged", "not_audited"):
                reasons.append(f"S7 leakage audit is {status}")
        if reasons:
            raise Refusal(RefusalCode.LIVE_GATE_FAILED, "; ".join(reasons), details={"strategy_id": strategy_id})

    async def _reference_price(self, session: Session, intent: OrderIntent) -> float | None:
        """Last known trade price at the session clock, from I2, used for notional and fat-finger bands."""
        ref = self.settings.risk.reference
        known_at = self.known_at(session)
        window = Window(start=known_at - timedelta(seconds=ref.lookback_s), end=known_at, clamped=False)
        dataset = self.settings.data.market_data_dataset_template.format(channel=ref.channel)
        try:
            fetched = await self._query(
                session,
                dataset=dataset,
                window=window,
                columns=None,
                filters=[PitFilter.eq("venue", intent.venue), PitFilter.eq("symbol", intent.symbol)],
                touch=False,
            )
        except (DownstreamUnavailable, DownstreamError) as exc:
            log.info("reference_price_unavailable", symbol=intent.symbol, error=str(exc))
            return None
        prices = [row[ref.price_column] for row in fetched.rows if isinstance(row.get(ref.price_column), int | float)]
        return float(prices[-1]) if prices else None

    async def _check_drawdown(self, session: Session, client: BrokerAdapter) -> None:
        account = await client.account(session.account_id, known_at=self._as_of(session))
        self._update_equity(session, account.equity if account.equity is not None else account.balance)
        usage = session.usage
        if usage.peak_equity is None or usage.last_equity is None:
            return
        breach = bud.drawdown_breach(session.limits, usage.peak_equity, usage.last_equity)
        if breach is not None and session.status is SessionStatus.ACTIVE:
            raise self._breach_refusal([breach], equity=usage.last_equity, peak_equity=usage.peak_equity)

    def _update_equity(self, session: Session, equity: float | None) -> None:
        if equity is None:
            return
        usage = session.usage
        usage.last_equity = equity
        usage.peak_equity = equity if usage.peak_equity is None else max(usage.peak_equity, equity)

    async def cancel_order(self, credentials: Credentials, *, order_id: str) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> CancelResult:
            self._require_trading(session)
            broker = self._broker(session)
            as_of = self._as_of(session)
            with refusing_downstream():
                existing = await broker.get(order_id, known_at=as_of)
                owner = (existing.model_extra or {}).get("account_id")
                if owner is not None and owner != session.account_id:
                    raise Refusal(RefusalCode.NOT_FOUND, f"no order {order_id} in this session's account")
                return CancelResult(order=await broker.cancel(order_id, known_at=as_of), routed_to=broker.routed_to)

        return await self.invoke("cancel_order", credentials, {"order_id": order_id}, impl)

    async def positions(self, credentials: Credentials) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> PositionsResult:
            self._require_trading(session)
            with refusing_downstream():
                positions: list[Position] = await self._broker(session).positions(
                    session.account_id, known_at=self._as_of(session)
                )
            return PositionsResult(account_id=session.account_id, positions=positions)

        return await self.invoke("positions", credentials, {}, impl)

    async def account(self, credentials: Credentials) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> AccountResult:
            self._require_trading(session)
            client = self._broker(session)
            with refusing_downstream():
                await self._check_drawdown(session, client)
                account = await client.account(session.account_id, known_at=self._as_of(session))
            usage = session.usage
            dd = (
                bud.drawdown(usage.peak_equity, usage.last_equity)
                if usage.peak_equity is not None and usage.last_equity is not None
                else None
            )
            return AccountResult(
                account=account,
                peak_equity=usage.peak_equity,
                drawdown=dd,
                max_drawdown=session.limits.max_drawdown,
            )

        return await self.invoke("account", credentials, {}, impl)

    async def instrument(self, credentials: Credentials, *, venue: str, symbol: str) -> BaseModel | RefusalBody:
        """Contract 6.8 trading rules (lot, tick, band, status) at the session's broker."""
        args = {"venue": venue, "symbol": symbol}

        async def impl(session: Session) -> InstrumentResult:
            broker = self._broker(session)
            with refusing_downstream():
                rules = await broker.instrument(venue, symbol, known_at=self._as_of(session))
            return InstrumentResult(broker=broker.name, rules=None if rules is None else rules.model_dump(mode="json"))

        return await self.invoke("instrument", credentials, args, impl)

    async def market_slice(
        self, credentials: Credentials, *, symbols: list[str] | None = None
    ) -> BaseModel | RefusalBody:
        """Contract 6.7 slice from I2 at the session clock: the same observation an RL agent and the terminal get."""
        args = {"symbols": symbols}

        async def impl(session: Session) -> MarketSliceResult:
            known_at = to_iso(self.known_at(session))
            if symbols:
                session.usage.touch_symbols(symbols)
            with refusing_downstream():
                data = await self.downstream.pit.slice(known_at, symbols)
            return MarketSliceResult(known_at=known_at, slice=data)

        return await self.invoke("market_slice", credentials, args, impl)

    async def budget(self, credentials: Credentials) -> BaseModel | RefusalBody:
        async def impl(session: Session) -> BudgetResult:
            return BudgetResult(
                status=session.status,
                freeze=session.freeze.model_dump(mode="json") if session.freeze else None,
                limits=session.limits,
                usage=self._totals(session),
                remaining=self._remaining(session),
                on_breach={k.value: v.value for k, v in self.settings.budgets.on_breach.items()},
            )

        return await self.invoke("budget", credentials, {}, impl)

    async def report_usage(
        self, credentials: Credentials, *, tokens_in: int, tokens_out: int, latency_ms: float, model: str
    ) -> BaseModel | RefusalBody:
        args = {"tokens_in": tokens_in, "tokens_out": tokens_out, "latency_ms": latency_ms, "model": model}
        pricing = self.settings.pricing

        async def impl(session: Session) -> UsageResult:
            if tokens_in < 0 or tokens_out < 0 or latency_ms < 0:
                raise Refusal(RefusalCode.INVALID_REQUEST, "tokens and latency must be non-negative")
            price = pricing.models.get(model)
            if price is None:
                if pricing.unknown_model_policy is UnknownModelPolicy.REFUSE:
                    raise Refusal(
                        RefusalCode.INVALID_REQUEST,
                        f"model {model!r} is not in the price table",
                        details={"models": sorted(pricing.models)},
                    )
                price = pricing.default
            cost = bud.token_cost_usd(price, tokens_in, tokens_out)
            usage = session.usage
            usage.tokens_in += tokens_in
            usage.tokens_out += tokens_out
            usage.token_cost_usd += cost
            usage.latency_total_ms += latency_ms
            usage.latency_max_ms = max(usage.latency_max_ms, latency_ms)
            usage.decisions += 1
            usage.add_model(model)
            breaches = bud.usage_breaches(session.limits, usage, latency_ms)
            if breaches:
                raise self._breach_refusal(breaches, cost_usd=cost)
            return UsageResult(
                model=model, cost_usd=cost, usage=self._totals(session), remaining=self._remaining(session)
            )

        return await self.invoke("report_usage", credentials, args, impl)
