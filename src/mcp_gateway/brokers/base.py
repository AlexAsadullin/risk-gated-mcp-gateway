"""The broker boundary (contract 6.8).

Every order and account call of the gateway goes through :class:`BrokerAdapter`. The MCP tools, budgets and
pre-trade checks never import a concrete adapter, so a new exchange or terminal is a new adapter class and one
config entry. Orders and results use the contract 6.4 shapes, with ``qty`` in shares (MOEX lots are converted by
the adapters that talk to a broker in lots).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from mcp_gateway.clients.orders import OrderClient
from mcp_gateway.contracts import Account, Order, OrderRequest, Position
from mcp_gateway.settings import InstrumentRule


class OrderNotFound(Exception):
    """The adapter has no order with this id (mapped to the ``not_found`` refusal)."""

    def __init__(self, order_id: str) -> None:
        super().__init__(f"no order {order_id}")
        self.order_id = order_id


class BrokerUnavailable(Exception):
    """The adapter cannot work right now (no token, no market data source)."""

    def __init__(self, broker: str, reason: str) -> None:
        super().__init__(f"{broker}: {reason}")
        self.broker = broker
        self.reason = reason


class InstrumentRules(BaseModel):
    """Trading rules of one instrument as a broker reports them (contract 6.8)."""

    venue: str
    symbol: str
    lot_size: float
    tick: float
    currency: str
    price_band_bps: float | None = None
    trading_status: str | None = None
    tradable: bool = True
    source: str = "config"


class BrokerAdapter(Protocol):
    """One execution venue. ``routed_to`` is the label reported to agents (``mirror``, ``i6``, ...)."""

    name: str
    routed_to: str

    async def place(self, request: OrderRequest, *, known_at: str | None) -> Order: ...

    async def get(self, order_id: str, *, known_at: str | None = None) -> Order: ...

    async def cancel(self, order_id: str, *, known_at: str | None = None) -> Order: ...

    async def account(self, account_id: str, *, known_at: str | None) -> Account: ...

    async def positions(self, account_id: str, *, known_at: str | None) -> list[Position]: ...

    async def instrument(self, venue: str, symbol: str, *, known_at: str | None = None) -> InstrumentRules | None: ...

    async def aclose(self) -> None: ...


class ConfiguredRules:
    """Instrument rules from ``brokers.instruments``; the fallback of every adapter without a live source."""

    def __init__(self, rules: list[InstrumentRule]) -> None:
        self._rules = {(r.venue, r.symbol): r for r in rules}

    def get(self, venue: str, symbol: str) -> InstrumentRules | None:
        rule = self._rules.get((venue, symbol))
        if rule is None:
            return None
        return InstrumentRules(
            venue=venue,
            symbol=symbol,
            lot_size=rule.lot_size,
            tick=rule.tick,
            currency=rule.currency,
            price_band_bps=rule.price_band_bps,
        )


class HttpOrderBroker:
    """A contract 6.4 order service (I6 shadow exchange, I7 execution engine) as a broker adapter."""

    def __init__(self, name: str, routed_to: str, client: OrderClient, rules: ConfiguredRules) -> None:
        self.name = name
        self.routed_to = routed_to
        self._client = client
        self._rules = rules

    async def place(self, request: OrderRequest, *, known_at: str | None) -> Order:
        return await self._client.place(request, known_at=known_at)

    async def get(self, order_id: str, *, known_at: str | None = None) -> Order:
        return await self._client.get(order_id)

    async def cancel(self, order_id: str, *, known_at: str | None = None) -> Order:
        return await self._client.cancel(order_id)

    async def account(self, account_id: str, *, known_at: str | None) -> Account:
        return await self._client.account(account_id, known_at=known_at)

    async def positions(self, account_id: str, *, known_at: str | None) -> list[Position]:
        return await self._client.positions(account_id, known_at=known_at)

    async def instrument(self, venue: str, symbol: str, *, known_at: str | None = None) -> InstrumentRules | None:
        return self._rules.get(venue, symbol)

    async def aclose(self) -> None:
        """The client belongs to :class:`~mcp_gateway.downstream.Downstream`, which closes it."""
