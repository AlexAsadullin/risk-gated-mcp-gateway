"""Unguarded pass-through baseline, a reimplementation of the laukikk/alpaca-mcp design for the report.

That analog is a thin wrapper that forwards each tool call to the broker or data API, without a session clock,
risk checks or budgets (spec I9 table: "Lightweight Alpaca wrapper; no risk management or validation"). This
class does the same over the platform contracts: data queries go to I2 in live mode with ``known_at`` equal to
the wall clock, backtests go to I5 with the window the agent asked for, and orders go straight to I6.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp_gateway.contracts import (
    Actor,
    BacktestRequest,
    BacktestResponse,
    Fidelity,
    Order,
    OrderRequest,
    OrderType,
    PitFilter,
    PitQuery,
    Side,
)
from mcp_gateway.downstream import Downstream
from mcp_gateway.settings import Settings
from mcp_gateway.timeutil import Clock, to_iso


class PassThroughWrapper:
    def __init__(self, settings: Settings, downstream: Downstream, clock: Clock, agent_id: str) -> None:
        self._settings = settings
        self._downstream = downstream
        self._clock = clock
        self._agent_id = agent_id
        self.account_id = settings.orders.account_id_template.format(agent_id=agent_id)

    async def market_data(self, *, venue: str, symbol: str, channel: str, start: str, end: str) -> list[dict[str, Any]]:
        query = PitQuery(
            dataset=self._settings.data.market_data_dataset_template.format(channel=channel),
            start=start,
            end=end,
            known_at=to_iso(self._clock.now()),
            filters=[PitFilter.eq("venue", venue), PitFilter.eq("symbol", symbol)],
            mode="live",
        )
        return (await self._downstream.pit.query(query)).rows

    async def run_backtest(
        self, *, strategy: str, params: dict[str, Any], universe: list[str], start: str, end: str, fidelity: Fidelity
    ) -> BacktestResponse:
        request = BacktestRequest(
            strategy=strategy,
            params=params,
            universe=universe,
            start=start,
            end=end,
            fidelity=fidelity,
            known_at=to_iso(self._clock.now()),
        )
        return await self._downstream.backtest.run(request)

    async def place_order(
        self, *, venue: str, symbol: str, side: Side, type: OrderType, qty: float, price: float | None, strategy_id: str
    ) -> Order:
        request = OrderRequest(
            client_order_id=str(uuid.uuid4()),
            account_id=self.account_id,
            venue=venue,
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            price=price,
            time_in_force=self._settings.orders.default_time_in_force,
            strategy_id=strategy_id,
            actor=Actor(kind="agent", id=self._agent_id),
        )
        return await self._downstream.shadow.place(request)

    async def report_usage(self) -> None:
        """The analog has no usage accounting, so a usage report changes nothing."""
