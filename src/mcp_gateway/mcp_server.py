"""MCP adapter: exposes exactly the contract-6.6 tools over the ``mcp`` SDK.

Each tool returns a ``CallToolResult``. On success its ``structuredContent`` matches the tool's output schema;
on refusal ``isError`` is true and ``structuredContent`` is a ``RefusalBody``. Agents never pass ``known_at``:
no tool has such a parameter, and the gateway injects the session clock.
"""

# No ``from __future__ import annotations`` here: the SDK reads these signatures at runtime to build schemas.

import json
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from mcp_gateway.contracts import Fidelity, OrderType, Side
from mcp_gateway.gateway import Credentials, Gateway
from mcp_gateway.outputs import (
    AccountResult,
    BacktestResult,
    BudgetResult,
    CancelResult,
    ClockResult,
    FeaturesResult,
    ForecastResult,
    MarketDataResult,
    OrderResult,
    PositionsResult,
    UsageResult,
    VerdictResult,
)
from mcp_gateway.refusals import RefusalBody
from mcp_gateway.settings import AuthSettings

TOOL_NAMES = (
    "clock",
    "market_data",
    "features",
    "forecast",
    "run_backtest",
    "verdict",
    "place_order",
    "cancel_order",
    "positions",
    "account",
    "budget",
    "report_usage",
)

INSTRUCTIONS = (
    "Tradeq risk-gated gateway. Call clock first: every data, feature, forecast and backtest answer is as known "
    "at the session clock, which you cannot change. Times are ISO-8601 UTC. Every backtest and order intent is "
    "logged as a trial in the research ledger. Refusals come back as errors with a 'code' field. Report your LLM "
    "usage after each decision with report_usage; budgets for capital, drawdown, tokens and latency apply, and a "
    "breach can freeze the session."
)

CredentialSource = Callable[[Context], Credentials]

ISO = Annotated[str, Field(description="ISO-8601 UTC timestamp, e.g. 2025-03-01T00:00:00Z.")]
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def header_credentials(auth: AuthSettings) -> CredentialSource:
    """Read ``Authorization: Bearer <key>`` (and the admin session override) from the HTTP request."""

    def source(ctx: Context) -> Credentials:
        headers: Mapping[str, str] = ctx.headers or {}
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = lowered.get(auth.api_key_header.lower())
        key = None
        if raw:
            scheme, _, token = raw.partition(" ")
            key = token.strip() if scheme.lower() == auth.bearer_scheme and token else raw.strip()
        return Credentials(api_key=key, session_override=lowered.get(auth.session_override_header.lower()))

    return source


def fixed_credentials(api_key: str | None, session_override: str | None = None) -> CredentialSource:
    """Credentials bound at start-up, for stdio and in-process transports that carry no headers."""

    def source(_: Context) -> Credentials:
        return Credentials(api_key=api_key, session_override=session_override)

    return source


def to_result(outcome: BaseModel) -> CallToolResult:
    data = outcome.model_dump(mode="json")
    text = [TextContent(type="text", text=json.dumps(data, separators=(",", ":")))]
    return CallToolResult(content=text, structured_content=data, is_error=isinstance(outcome, RefusalBody))


def build_mcp_server(gateway: Gateway, credentials: CredentialSource) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        name="tradeq-risk-gated-gateway", instructions=INSTRUCTIONS, version="0.1.0", warn_on_duplicate_tools=True
    )

    @server.tool(description="Session clock (known_at), mode and status. Call first.", annotations=READ_ONLY)
    async def clock(ctx: Context) -> Annotated[CallToolResult, ClockResult]:
        return to_result(await gateway.clock_tool(credentials(ctx)))

    @server.tool(
        description=(
            "Market tape rows (trades, book_delta, book_snapshot, funding, liquidations, open_interest) for one "
            "venue and symbol, as known at the session clock. end must be <= known_at. Returns the latest rows "
            "up to the row cap; prices in quote currency, sizes in base units."
        ),
        annotations=READ_ONLY,
    )
    async def market_data(
        ctx: Context,
        venue: Annotated[str, Field(description="Venue id, e.g. binance.")],
        symbol: Annotated[str, Field(description="Normalised symbol BASE-QUOTE-KIND, e.g. BTC-USDT-PERP.")],
        channel: Annotated[str, Field(description="Tape channel, e.g. trades.")],
        start: ISO,
        end: ISO,
    ) -> Annotated[CallToolResult, MarketDataResult]:
        outcome = await gateway.market_data(
            credentials(ctx), venue=venue, symbol=symbol, channel=channel, start=start, end=end
        )
        return to_result(outcome)

    @server.tool(
        description="Point-in-time feature values for symbols over [start, end], as known at the session clock.",
        annotations=READ_ONLY,
    )
    async def features(
        ctx: Context,
        names: Annotated[list[str], Field(description="Feature names.", min_length=1)],
        symbols: Annotated[list[str], Field(description="Normalised symbols.", min_length=1)],
        start: ISO,
        end: ISO,
    ) -> Annotated[CallToolResult, FeaturesResult]:
        return to_result(await gateway.features(credentials(ctx), names=names, symbols=symbols, start=start, end=end))

    @server.tool(
        description=(
            "Forecast of returns from a time-series model (S1) made at the session clock. mean and quantiles are "
            "fractional returns over the horizon (0.01 = 1%); horizon is in model bars."
        ),
        annotations=READ_ONLY,
    )
    async def forecast(
        ctx: Context,
        model: Annotated[str, Field(description="Forecast model id.")],
        symbols: Annotated[list[str], Field(description="Normalised symbols.", min_length=1)],
        horizon: Annotated[int, Field(description="Steps ahead, in bars.", ge=1)],
    ) -> Annotated[CallToolResult, ForecastResult]:
        return to_result(await gateway.forecast(credentials(ctx), model=model, symbols=symbols, horizon=horizon))

    @server.tool(
        description=(
            "Run a backtest (I5) of a registered strategy over [start, end] with data as known at the session "
            "clock. Every call, including refused ones, is logged as a trial in the research ledger (S6)."
        )
    )
    async def run_backtest(
        ctx: Context,
        strategy: Annotated[str, Field(description="Strategy name registered in the backtest service.")],
        params: Annotated[dict[str, Any], Field(description="Strategy parameters; include 'seed' if any.")],
        universe: Annotated[list[str], Field(description="Normalised symbols to trade.", min_length=1)],
        start: ISO,
        end: ISO,
        fidelity: Annotated[Fidelity, Field(description="vectorised (fast) to reactive_sim (slow, realistic).")],
    ) -> Annotated[CallToolResult, BacktestResult]:
        outcome = await gateway.run_backtest(
            credentials(ctx),
            strategy=strategy,
            params=params,
            universe=universe,
            start=start,
            end=end,
            fidelity=fidelity,
        )
        return to_result(outcome)

    @server.tool(
        description=(
            "Ledger verdict (S6) for a strategy: trial count, Sharpe, PSR, DSR, PBO, pass/fail, plus the S7 "
            "leakage audit when this session used an LLM. promotable is true only if both pass."
        ),
        annotations=READ_ONLY,
    )
    async def verdict(
        ctx: Context, strategy_id: Annotated[str, Field(description="Your strategy name.")]
    ) -> Annotated[CallToolResult, VerdictResult]:
        return to_result(await gateway.verdict(credentials(ctx), strategy_id=strategy_id))

    @server.tool(
        description=(
            "Submit an order (paper sessions go to the shadow exchange I6, live to I7). Pre-trade checks: "
            "whitelist, fat-finger price band, order and position notional, capital, rate and drawdown budgets. "
            "qty in base units, price in quote currency (limit orders only). Logged as a trial."
        ),
        annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True),
    )
    async def place_order(
        ctx: Context,
        venue: Annotated[str, Field(description="Venue id, e.g. binance.")],
        symbol: Annotated[str, Field(description="Normalised symbol, e.g. BTC-USDT-PERP.")],
        side: Side,
        type: OrderType,
        qty: Annotated[float, Field(description="Quantity in base units.", gt=0)],
        strategy_id: Annotated[str, Field(description="Your strategy name.")],
        rationale: Annotated[str, Field(description="One or two sentences on why.", min_length=1, max_length=2000)],
        price: Annotated[float | None, Field(description="Limit price, quote currency; omit for market.")] = None,
    ) -> Annotated[CallToolResult, OrderResult]:
        outcome = await gateway.place_order(
            credentials(ctx),
            venue=venue,
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            price=price,
            strategy_id=strategy_id,
            rationale=rationale,
        )
        return to_result(outcome)

    @server.tool(description="Cancel an open order of this session's account.")
    async def cancel_order(
        ctx: Context, order_id: Annotated[str, Field(description="order_id from place_order.")]
    ) -> Annotated[CallToolResult, CancelResult]:
        return to_result(await gateway.cancel_order(credentials(ctx), order_id=order_id))

    @server.tool(description="Open positions of this session's account (qty in base units).", annotations=READ_ONLY)
    async def positions(ctx: Context) -> Annotated[CallToolResult, PositionsResult]:
        return to_result(await gateway.positions(credentials(ctx)))

    @server.tool(description="Account equity and drawdown from peak (fraction).", annotations=READ_ONLY)
    async def account(ctx: Context) -> Annotated[CallToolResult, AccountResult]:
        return to_result(await gateway.account(credentials(ctx)))

    @server.tool(description="Budget limits, usage so far, headroom and freeze state.", annotations=READ_ONLY)
    async def budget(ctx: Context) -> Annotated[CallToolResult, BudgetResult]:
        return to_result(await gateway.budget(credentials(ctx)))

    @server.tool(
        description=(
            "Report LLM usage for one decision: tokens in/out, decision latency in ms and model id. Cost is "
            "priced in USD from the gateway table. A breach of a token, spend or latency budget freezes the session."
        )
    )
    async def report_usage(
        ctx: Context,
        tokens_in: Annotated[int, Field(description="Prompt tokens.", ge=0)],
        tokens_out: Annotated[int, Field(description="Completion tokens.", ge=0)],
        latency_ms: Annotated[float, Field(description="Decision latency, milliseconds.", ge=0)],
        model: Annotated[str, Field(description="Model id, e.g. claude-sonnet-4-5.")],
    ) -> Annotated[CallToolResult, UsageResult]:
        outcome = await gateway.report_usage(
            credentials(ctx), tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms, model=model
        )
        return to_result(outcome)

    return server
