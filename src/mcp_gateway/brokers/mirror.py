"""The mirror sandbox: paper trading against a copy of the real market, with no broker involved.

The mirror reads the real MOEX order book and trade tape from the Point-in-time Store (I2) at the session clock
and matches orders against them, so an agent or an RL policy trades the live market (paper mode at "now") or a
replayed day (session clock in the past) under the same rules. It is the first adapter of contract 6.8; external
brokers (T-Bank sandbox, I6, I7) plug in beside it.

Matching rules (the market itself never sees these orders):

1. A market order, or the marketable part of a limit order, walks the opposite side of the latest book known at
   the clock, level by level. Liquidity it takes is remembered per book snapshot, so two orders against the same
   snapshot cannot both take the same shares. What the book cannot fill is cancelled (market, IOC), refused as a
   whole (FOK), or rests (GTC limit). A POST_ONLY order that would trade is rejected.
2. A resting limit order enters the queue behind the size already displayed at its price. On every later call
   the trades printed since the last check are replayed: a trade through the price fills the order in full, and
   a trade at the price on the opposite side first reduces the queue ahead, then fills the order.
3. Quantities must be whole lots and prices must sit on the tick (instrument rules from config); the fee is
   ``taker_fee_bps`` for liquidity taken and ``maker_fee_bps`` for resting fills.
4. Accounts start with ``initial_cash`` in roubles; equity marks positions at the mid of the latest book.

State (accounts, orders, positions) is kept in memory and written to a JSON file after every change, so a
gateway restart keeps the sandbox. Float64 is used for prices and cash, as in the rest of the gateway's risk
arithmetic; this is a simulator, not an accounting ledger.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp_gateway.brokers.base import ConfiguredRules, InstrumentRules, OrderNotFound
from mcp_gateway.clients.pit import PitClient
from mcp_gateway.contracts import Account, Order, OrderRequest, OrderStatus, PitFilter, PitQuery, Position
from mcp_gateway.risk import on_grid
from mcp_gateway.settings import MirrorSettings
from mcp_gateway.timeutil import NS_PER_SECOND, ns_to_iso, parse_iso, to_ns

BPS = 10_000.0
EPS = 1e-9
TERMINAL: frozenset[str] = frozenset({"filled", "cancelled", "rejected"})


@dataclass(frozen=True)
class BookView:
    """Best-first price levels of one snapshot; sizes in shares."""

    snapshot_id: str
    ts_ns: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2
        return None


@dataclass(frozen=True)
class TradePrint:
    ts_ns: int
    price: float
    size: float
    side: str  # aggressor side


class MarketSource(Protocol):
    async def book(self, venue: str, symbol: str, known_at: str) -> BookView | None: ...

    async def trades(self, venue: str, symbol: str, start: str, end: str, known_at: str) -> list[TradePrint]: ...


def _ns(value: Any) -> int:
    return to_ns(parse_iso(value)) if isinstance(value, str) else int(value)


class PitMarketSource:
    """Reads ``tape.book_snapshot`` and ``tape.trades`` from I2 in live mode at the given clock."""

    def __init__(self, pit: PitClient, cfg: MirrorSettings) -> None:
        self._pit = pit
        self._cfg = cfg

    async def book(self, venue: str, symbol: str, known_at: str) -> BookView | None:
        end_ns = to_ns(parse_iso(known_at))
        start = ns_to_iso(end_ns - round(self._cfg.book_lookback_s * NS_PER_SECOND))
        result = await self._pit.query(
            PitQuery(
                dataset=self._cfg.book_dataset,
                start=start,
                end=known_at,
                known_at=known_at,
                filters=[PitFilter.eq("venue", venue), PitFilter.eq("symbol", symbol)],
                mode="live",
            )
        )
        if not result.rows:
            return None
        latest = max(result.rows, key=lambda r: (_ns(r["valid_time"]), _ns(r["knowledge_time"])))
        if end_ns - _ns(latest["knowledge_time"]) > round(self._cfg.max_book_age_s * NS_PER_SECOND):
            return None
        rows = [r for r in result.rows if r["snapshot_id"] == latest["snapshot_id"]]
        bids = sorted(((float(r["price"]), float(r["size"])) for r in rows if r["side"] == "bid"), key=lambda x: -x[0])
        asks = sorted(((float(r["price"]), float(r["size"])) for r in rows if r["side"] == "ask"), key=lambda x: x[0])
        return BookView(str(latest["snapshot_id"]), _ns(latest["valid_time"]), bids, asks)

    async def trades(self, venue: str, symbol: str, start: str, end: str, known_at: str) -> list[TradePrint]:
        if _ns(start) >= _ns(end):
            return []
        result = await self._pit.query(
            PitQuery(
                dataset=self._cfg.trades_dataset,
                start=start,
                end=end,
                known_at=known_at,
                filters=[PitFilter.eq("venue", venue), PitFilter.eq("symbol", symbol)],
                mode="live",
            )
        )
        prints = [
            TradePrint(_ns(r["valid_time"]), float(r["price"]), float(r["size"]), str(r.get("side", "")))
            for r in result.rows
        ]
        return sorted(prints, key=lambda p: p.ts_ns)


@dataclass
class MirrorFill:
    ts_ns: int
    price: float
    qty: float
    liquidity: str
    fee: float


@dataclass
class MirrorOrder:
    order_id: str
    client_order_id: str
    account_id: str
    venue: str
    symbol: str
    side: str
    type: str
    qty: float
    price: float | None
    time_in_force: str
    status: str
    created_ns: int
    last_sync_ns: int
    filled: float = 0.0
    queue_ahead: float = 0.0
    reject_reason: str | None = None
    fills: list[MirrorFill] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return self.qty - self.filled

    def avg_price(self) -> float | None:
        if self.filled <= 0:
            return None
        return sum(f.price * f.qty for f in self.fills) / self.filled

    def wire(self) -> Order:
        return Order(
            order_id=self.order_id,
            status=order_status(self.status),
            reject_reason=self.reject_reason,
            client_order_id=self.client_order_id,
            venue=self.venue,
            symbol=self.symbol,
            side="buy" if self.side == "buy" else "sell",
            qty=self.qty,
            price=self.price,
            account_id=self.account_id,
            filled_qty=self.filled,
            avg_price=self.avg_price(),
            fee=sum(f.fee for f in self.fills),
            broker="mirror",
        )


@dataclass
class MirrorPosition:
    qty: float = 0.0
    avg_price: float = 0.0
    last_price: float | None = None


@dataclass
class MirrorAccount:
    cash: float
    currency: str
    positions: dict[str, MirrorPosition] = field(default_factory=dict)
    fees: float = 0.0


def _key(venue: str, symbol: str) -> str:
    return f"{venue}|{symbol}"


class MirrorBroker:
    name = "mirror"
    routed_to = "mirror"

    def __init__(
        self,
        source: MarketSource,
        rules: ConfiguredRules,
        cfg: MirrorSettings,
        state_path: Path | None,
        clock_ns: Any,
    ) -> None:
        self._source = source
        self._rules = rules
        self._cfg = cfg
        self._state_path = state_path
        self._clock_ns = clock_ns
        self._accounts: dict[str, MirrorAccount] = {}
        self._orders: dict[str, MirrorOrder] = {}
        self._consumed: dict[str, dict[float, float]] = {}
        self._load()

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text())
        for account_id, raw in data.get("accounts", {}).items():
            positions = {k: MirrorPosition(**v) for k, v in raw.pop("positions", {}).items()}
            self._accounts[account_id] = MirrorAccount(**raw, positions=positions)
        for order_id, raw in data.get("orders", {}).items():
            fills = [MirrorFill(**f) for f in raw.pop("fills", [])]
            self._orders[order_id] = MirrorOrder(**raw, fills=fills)

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "accounts": {k: asdict(v) for k, v in self._accounts.items()},
            "orders": {k: asdict(v) for k, v in self._orders.items()},
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, self._state_path)

    # ------------------------------------------------------------------ helpers

    def _now(self, known_at: str | None) -> tuple[str, int]:
        ns = to_ns(parse_iso(known_at)) if known_at else int(self._clock_ns())
        return ns_to_iso(ns), ns

    def _account(self, account_id: str) -> MirrorAccount:
        if account_id not in self._accounts:
            self._accounts[account_id] = MirrorAccount(cash=self._cfg.initial_cash, currency=self._cfg.currency)
        return self._accounts[account_id]

    def _apply_fill(self, order: MirrorOrder, price: float, qty: float, ts_ns: int, liquidity: str) -> None:
        fee_bps = self._cfg.taker_fee_bps if liquidity == "taker" else self._cfg.maker_fee_bps
        fee = price * qty * fee_bps / BPS
        order.fills.append(MirrorFill(ts_ns, price, qty, liquidity, fee))
        order.filled += qty
        account = self._account(order.account_id)
        position = account.positions.setdefault(_key(order.venue, order.symbol), MirrorPosition())
        signed = qty if order.side == "buy" else -qty
        new_qty = position.qty + signed
        if position.qty == 0 or (position.qty > 0) == (signed > 0):
            # Adding to (or opening) a position: the average price is volume-weighted.
            total = abs(position.qty) + qty
            position.avg_price = (position.avg_price * abs(position.qty) + price * qty) / total
        elif abs(signed) > abs(position.qty):
            position.avg_price = price  # flipped through zero: the remainder opens at this price
        if abs(new_qty) < EPS:
            new_qty, position.avg_price = 0.0, 0.0
        position.qty = new_qty
        position.last_price = price
        account.cash -= signed * price + fee
        account.fees += fee
        if order.remaining <= EPS:
            order.status = "filled"
        else:
            order.status = "partially_filled"

    def _take(self, order: MirrorOrder, book: BookView, limit: float | None, ts_ns: int) -> float:
        """Walk the opposite side up to ``limit``; returns the quantity filled."""
        levels = book.asks if order.side == "buy" else book.bids
        used = self._consumed.setdefault(f"{_key(order.venue, order.symbol)}|{book.snapshot_id}", {})
        filled = 0.0
        for price, size in levels:
            if limit is not None and (price > limit if order.side == "buy" else price < limit):
                break
            available = size - used.get(price, 0.0)
            take = min(available, order.remaining)
            if take <= EPS:
                continue
            used[price] = used.get(price, 0.0) + take
            self._apply_fill(order, price, take, ts_ns, "taker")
            filled += take
            if order.remaining <= EPS:
                break
        return filled

    @staticmethod
    def _fillable(order: MirrorOrder, book: BookView, limit: float | None) -> float:
        levels = book.asks if order.side == "buy" else book.bids
        return sum(
            size
            for price, size in levels
            if limit is None or (price <= limit if order.side == "buy" else price >= limit)
        )

    def _reject(self, order: MirrorOrder, reason: str) -> Order:
        order.status, order.reject_reason = "rejected", reason
        self._orders[order.order_id] = order
        self._save()
        return order.wire()

    # ------------------------------------------------------------------ resting orders

    async def _sync(self, order: MirrorOrder, now_iso: str, now_ns: int) -> None:
        """Replay trades printed since the last check against one resting limit order (rule 2)."""
        if order.status in TERMINAL or order.price is None or now_ns <= order.last_sync_ns:
            return
        prints = await self._source.trades(
            order.venue, order.symbol, ns_to_iso(order.last_sync_ns + 1), now_iso, now_iso
        )
        price = order.price
        for p in prints:
            if order.remaining <= EPS:
                break
            through = p.price < price if order.side == "buy" else p.price > price
            at_level = math.isclose(p.price, price) and p.side != order.side
            if through:
                self._apply_fill(order, price, order.remaining, p.ts_ns, "maker")
            elif at_level:
                eat = p.size
                if order.queue_ahead > 0:
                    absorbed = min(order.queue_ahead, eat)
                    order.queue_ahead -= absorbed
                    eat -= absorbed
                if eat > EPS:
                    self._apply_fill(order, price, min(eat, order.remaining), p.ts_ns, "maker")
        order.last_sync_ns = now_ns

    async def _sync_account(self, account_id: str, now_iso: str, now_ns: int) -> None:
        changed = False
        for order in self._orders.values():
            if order.account_id == account_id and order.status not in TERMINAL:
                before = order.filled
                await self._sync(order, now_iso, now_ns)
                changed = changed or order.filled != before
        if changed:
            self._save()

    # ------------------------------------------------------------------ BrokerAdapter

    async def place(self, request: OrderRequest, *, known_at: str | None) -> Order:
        now_iso, now_ns = self._now(known_at)
        await self._sync_account(request.account_id, now_iso, now_ns)
        order = MirrorOrder(
            order_id=f"mir-{uuid.uuid4().hex[:16]}",
            client_order_id=request.client_order_id,
            account_id=request.account_id,
            venue=request.venue,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            qty=request.qty,
            price=request.price,
            time_in_force=request.time_in_force,
            status="accepted",
            created_ns=now_ns,
            last_sync_ns=now_ns,
        )
        rules = self._rules.get(request.venue, request.symbol)
        if rules is None:
            return self._reject(order, "unknown_instrument")
        if not on_grid(request.qty, rules.lot_size) or request.qty <= 0:
            return self._reject(order, "qty_not_whole_lots")
        if request.price is not None and not on_grid(request.price, rules.tick):
            return self._reject(order, "price_off_tick")
        book = await self._source.book(request.venue, request.symbol, now_iso)
        if book is None:
            return self._reject(order, "no_market_data")
        limit = request.price if request.type == "limit" else None
        crosses = self._fillable(order, book, limit) > EPS
        if request.time_in_force == "POST_ONLY" and crosses:
            return self._reject(order, "post_only_would_trade")
        if request.time_in_force == "FOK" and self._fillable(order, book, limit) + EPS < request.qty:
            return self._reject(order, "fok_not_fillable")
        self._account(request.account_id)
        self._take(order, book, limit, book.ts_ns)
        if order.remaining > EPS:
            if request.type == "market" or request.time_in_force in ("IOC", "FOK"):
                # The unfilled rest is cancelled; ``filled_qty`` reports what traded.
                order.status = "cancelled"
                order.reject_reason = "insufficient_liquidity"
            else:
                own = book.bids if order.side == "buy" else book.asks
                order.queue_ahead = sum(size for price, size in own if math.isclose(price, request.price or 0.0))
        self._orders[order.order_id] = order
        self._save()
        return order.wire()

    async def get(self, order_id: str, *, known_at: str | None = None) -> Order:
        order = self._find(order_id)
        now_iso, now_ns = self._now(known_at)
        await self._sync(order, now_iso, now_ns)
        self._save()
        return order.wire()

    async def cancel(self, order_id: str, *, known_at: str | None = None) -> Order:
        order = self._find(order_id)
        now_iso, now_ns = self._now(known_at)
        await self._sync(order, now_iso, now_ns)
        if order.status not in TERMINAL:
            order.status = "cancelled"
        self._save()
        return order.wire()

    def _find(self, order_id: str) -> MirrorOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order

    async def _marks(self, account: MirrorAccount, now_iso: str) -> dict[str, float | None]:
        marks: dict[str, float | None] = {}
        for key, position in account.positions.items():
            venue, symbol = key.split("|", 1)
            book = await self._source.book(venue, symbol, now_iso)
            mid = book.mid() if book else None
            marks[key] = mid if mid is not None else position.last_price
        return marks

    async def positions(self, account_id: str, *, known_at: str | None) -> list[Position]:
        now_iso, now_ns = self._now(known_at)
        await self._sync_account(account_id, now_iso, now_ns)
        account = self._account(account_id)
        marks = await self._marks(account, now_iso)
        out: list[Position] = []
        for key, p in account.positions.items():
            if p.qty == 0:
                continue
            venue, symbol = key.split("|", 1)
            mark = marks.get(key)
            out.append(
                Position(
                    venue=venue,
                    symbol=symbol,
                    qty=p.qty,
                    avg_price=p.avg_price,
                    mark_price=mark,
                    notional=None if mark is None else p.qty * mark,
                )
            )
        return out

    async def account(self, account_id: str, *, known_at: str | None) -> Account:
        now_iso, now_ns = self._now(known_at)
        await self._sync_account(account_id, now_iso, now_ns)
        account = self._account(account_id)
        marks = await self._marks(account, now_iso)
        value = sum(p.qty * (marks.get(k) or p.last_price or 0.0) for k, p in account.positions.items())
        return Account(
            account_id=account_id,
            equity=account.cash + value,
            balance=account.cash,
            currency=account.currency,
            fees=account.fees,
            broker="mirror",
        )

    async def instrument(self, venue: str, symbol: str, *, known_at: str | None = None) -> InstrumentRules | None:
        return self._rules.get(venue, symbol)

    def order_count(self) -> int:
        return len(self._orders)

    async def aclose(self) -> None:
        self._save()


def order_status(value: str) -> OrderStatus:
    """Narrow a stored status string to the contract literal (for typed callers)."""
    allowed: tuple[OrderStatus, ...] = ("accepted", "rejected", "partially_filled", "filled", "cancelled")
    if value not in allowed:
        raise ValueError(f"unknown status {value!r}")
    return value
