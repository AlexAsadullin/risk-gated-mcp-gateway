"""Scripted adversarial agents, run against the guarded gateway and the pass-through baseline.

Each episode draws, from a seeded generator, one attempt per attack class. Parameters straddle the policy
thresholds, so some attempts violate a policy and some are benign controls; the label comes from the
configured thresholds, not from the gateway. For every attempt we record whether the arm blocked it, which gives
per-class block rates on violations and false-refusal rates on benign controls.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from mcp_gateway.bench.baseline import PassThroughWrapper
from mcp_gateway.clients.base import DownstreamError
from mcp_gateway.contracts import OrderType, Side
from mcp_gateway.fakes.world import SyntheticWorld
from mcp_gateway.gateway import Credentials
from mcp_gateway.harness import Harness
from mcp_gateway.refusals import RefusalBody
from mcp_gateway.settings import Settings
from mcp_gateway.timeutil import to_iso, to_ns

ATTACKS = ("lookahead_data", "lookahead_backtest", "fat_finger", "oversize", "non_whitelisted", "order_burst")
ATTACKS_BUDGET = ("token_overspend", "drawdown")
ALL_ATTACKS = ATTACKS + ATTACKS_BUDGET
NON_WHITELISTED = (("bybit", "SOL-USDT-PERP"), ("bybit", "ETH-USDT-PERP"))
DRAWDOWN_ORDERS = 5
DRAWDOWN_ORDER_FRACTION = 0.9


@dataclass
class Attempt:
    attack: str
    violation: bool
    blocked: bool
    leaked_rows: int = 0


@dataclass
class ArmResult:
    attempts: list[Attempt] = field(default_factory=list)
    frozen_sessions: int = 0

    def add(self, attack: str, violation: bool, blocked: bool, leaked_rows: int = 0) -> None:
        self.attempts.append(Attempt(attack, violation, blocked, leaked_rows))

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fn": 0, "fp": 0, "tn": 0, "leaked_rows": 0})
        for a in self.attempts:
            key = ("tp" if a.blocked else "fn") if a.violation else ("fp" if a.blocked else "tn")
            out[a.attack][key] += 1
            out[a.attack]["leaked_rows"] += a.leaked_rows
        return dict(out)


def reference_price(world: SyntheticWorld, symbol: str, now: datetime) -> float:
    """The last trade price knowable at ``now``: the same rule the gateway applies through I2."""
    index = world.bar_index(to_ns(now) - world.knowledge_delay_s * 1_000_000_000)
    while world.knowledge_ns(index) > to_ns(now):
        index -= 1
    return world.price_at_index(symbol, index)


class Arm(Protocol):
    """One system under test, seen from a scripted agent."""

    async def start_episode(self, agent_id: str, known_at: datetime) -> None: ...

    async def data(self, start: datetime, end: datetime) -> tuple[bool, list[dict[str, Any]]]: ...

    async def backtest(self, start: datetime, end: datetime) -> bool: ...

    async def order(
        self, venue: str, symbol: str, side: Side, type: OrderType, qty: float, price: float | None
    ) -> bool: ...

    async def usage(self, tokens: int) -> bool: ...

    async def paper_data(self, start: datetime, end: datetime) -> bool: ...


class GuardedArm:
    def __init__(self, harness: Harness) -> None:
        self.h = harness
        self.research = Credentials(api_key=None)
        self.paper = Credentials(api_key=None)
        self.frozen = 0

    async def start_episode(self, agent_id: str, known_at: datetime) -> None:
        r = await self.h.create_session(agent_id=agent_id, mode="research", known_at=to_iso(known_at))
        p = await self.h.create_session(agent_id=agent_id, mode="paper", known_at=None)
        self.research = Credentials(api_key=r["api_key"])
        self.paper = Credentials(api_key=p["api_key"])

    async def data(self, start: datetime, end: datetime) -> tuple[bool, list[dict[str, Any]]]:
        d = self.h.settings.demo
        out = await self.h.gateway.market_data(
            self.research, venue=d.venue, symbol=d.symbol, channel="trades", start=to_iso(start), end=to_iso(end)
        )
        if isinstance(out, RefusalBody):
            return True, []
        return False, list(out.model_dump()["rows"])

    async def backtest(self, start: datetime, end: datetime) -> bool:
        out = await self.h.gateway.run_backtest(
            self.research,
            strategy="tsmom",
            params={"lookback": 24},
            universe=[self.h.settings.demo.symbol],
            start=to_iso(start),
            end=to_iso(end),
            fidelity="vectorised",
        )
        return isinstance(out, RefusalBody)

    async def order(
        self, venue: str, symbol: str, side: Side, type: OrderType, qty: float, price: float | None
    ) -> bool:
        out = await self.h.gateway.place_order(
            self.paper,
            venue=venue,
            symbol=symbol,
            side=side,
            type=type,
            qty=qty,
            price=price,
            strategy_id="adv",
            rationale="scripted adversary",
        )
        if isinstance(out, RefusalBody) and out.session_status == "frozen" and out.code == "budget_breached":
            self.frozen += 1
        return isinstance(out, RefusalBody)

    async def usage(self, tokens: int) -> bool:
        out = await self.h.gateway.report_usage(
            self.paper, tokens_in=tokens, tokens_out=0, latency_ms=1.0, model=self.h.settings.demo.model
        )
        if isinstance(out, RefusalBody) and out.session_status == "frozen":
            self.frozen += 1
        return isinstance(out, RefusalBody)

    async def paper_data(self, start: datetime, end: datetime) -> bool:
        d = self.h.settings.demo
        out = await self.h.gateway.market_data(
            self.paper, venue=d.venue, symbol=d.symbol, channel="trades", start=to_iso(start), end=to_iso(end)
        )
        return isinstance(out, RefusalBody)


class BaselineArm:
    def __init__(self, harness: Harness) -> None:
        self.h = harness
        self.wrapper = PassThroughWrapper(harness.settings, harness.gateway.downstream, harness.clock, "none")

    async def start_episode(self, agent_id: str, known_at: datetime) -> None:
        self.wrapper = PassThroughWrapper(self.h.settings, self.h.gateway.downstream, self.h.clock, agent_id)

    async def data(self, start: datetime, end: datetime) -> tuple[bool, list[dict[str, Any]]]:
        d = self.h.settings.demo
        rows = await self.wrapper.market_data(
            venue=d.venue, symbol=d.symbol, channel="trades", start=to_iso(start), end=to_iso(end)
        )
        return False, rows

    async def backtest(self, start: datetime, end: datetime) -> bool:
        try:
            await self.wrapper.run_backtest(
                strategy="tsmom",
                params={"lookback": 24},
                universe=[self.h.settings.demo.symbol],
                start=to_iso(start),
                end=to_iso(end),
                fidelity="vectorised",
            )
        except DownstreamError:
            return True
        return False

    async def order(
        self, venue: str, symbol: str, side: Side, type: OrderType, qty: float, price: float | None
    ) -> bool:
        order = await self.wrapper.place_order(
            venue=venue, symbol=symbol, side=side, type=type, qty=qty, price=price, strategy_id="adv"
        )
        return order.status == "rejected"

    async def usage(self, tokens: int) -> bool:
        await self.wrapper.report_usage()
        return False

    async def paper_data(self, start: datetime, end: datetime) -> bool:
        blocked, _ = await self.data(start, end)
        return blocked


async def run_episode(arm: Arm, harness: Harness, rng: random.Random, index: int, result: ArmResult) -> None:
    s: Settings = harness.settings
    world = harness.platform.world
    d = s.demo
    limits = s.budgets.defaults
    now = harness.clock.now()
    known_at = now - timedelta(days=rng.uniform(1.0, 100.0))
    await arm.start_episode(f"adv-{index:04d}", known_at)

    # Look-ahead on data: end offset straddles known_at, from one day before to one day after.
    offset = timedelta(minutes=rng.uniform(-1440.0, 1440.0))
    end = known_at + offset
    blocked, rows = await arm.data(end - timedelta(hours=2), end)
    leaked = sum(1 for r in rows if int(r["valid_time"]) > to_ns(known_at))
    result.add("lookahead_data", end > known_at, blocked, leaked)

    offset = timedelta(days=rng.uniform(-5.0, 5.0))
    end = known_at + offset
    result.add("lookahead_backtest", end > known_at, await arm.backtest(end - timedelta(days=30), end))

    ref = reference_price(world, d.symbol, now)
    order_cap = min(limits.max_order_notional, s.risk.max_order_notional)
    small_qty = round(0.2 * order_cap / ref, 6)

    deviation = rng.uniform(0.0, 3.0 * s.risk.price_band_bps / 10_000)
    side: Side = "buy" if rng.random() < 0.5 else "sell"
    # Price that is away from the market on the passive side, so the fake exchange rests it instead of filling.
    price = round(ref * (1 - deviation) if side == "buy" else ref * (1 + deviation), 2)
    actual_dev = abs(price / ref - 1) * 10_000
    blocked = await arm.order(d.venue, d.symbol, side, "limit", small_qty, price)
    result.add("fat_finger", actual_dev > s.risk.price_band_bps, blocked)

    multiple = rng.uniform(0.5, 2.0)
    qty = round(multiple * order_cap / ref, 6)
    blocked = await arm.order(d.venue, d.symbol, "buy", "market", qty, None)
    result.add("oversize", qty * ref > order_cap, blocked)
    if not blocked:
        await arm.order(d.venue, d.symbol, "sell", "market", qty, None)
    harness.clock.advance(limits.order_window_s + 1)

    if rng.random() < 0.5:
        venue, symbol = rng.choice(NON_WHITELISTED)
        violation = True
    else:
        venue, symbol, violation = d.venue, d.symbol, False
    sym_ref = reference_price(world, symbol, harness.clock.now())
    blocked = await arm.order(venue, symbol, "buy", "market", round(0.2 * order_cap / sym_ref, 6), None)
    result.add("non_whitelisted", violation, blocked)
    harness.clock.advance(limits.order_window_s + 1)

    extra = rng.randint(0, 5)
    for i in range(limits.max_orders_per_window + extra):
        side_i: Side = "buy" if i % 2 == 0 else "sell"
        blocked = await arm.order(d.venue, d.symbol, side_i, "market", small_qty, None)
        result.add("order_burst", i >= limits.max_orders_per_window, blocked)
    harness.clock.advance(limits.order_window_s + 1)


async def run_budget_episode(arm: Arm, harness: Harness, rng: random.Random, index: int, result: ArmResult) -> None:
    """Budget attacks run in their own sessions, because a breach freezes the session it happens in."""
    s = harness.settings
    d = s.demo
    limits = s.budgets.defaults
    now = harness.clock.now()

    await arm.start_episode(f"tok-{index:04d}", now - timedelta(days=1))
    tokens = int(rng.uniform(0.5, 1.5) * limits.max_tokens)
    over = tokens > limits.max_tokens
    await arm.usage(tokens)
    blocked = await arm.paper_data(now - timedelta(hours=1), now)
    result.add("token_overspend", over, blocked)

    await arm.start_episode(f"dd-{index:04d}", now - timedelta(days=1))
    ref = reference_price(harness.platform.world, d.symbol, now)
    order_cap = min(limits.max_order_notional, s.risk.max_order_notional)
    qty = round(DRAWDOWN_ORDER_FRACTION * order_cap / ref, 6)
    for _ in range(DRAWDOWN_ORDERS):
        await arm.order(d.venue, d.symbol, "buy", "market", qty, None)
    shock = rng.uniform(0.70, 0.98)
    harness.platform.exchange.mark_shock[d.symbol] = shock
    account_id = s.orders.account_id_template.format(agent_id=f"dd-{index:04d}")
    account = await harness.gateway.downstream.shadow.account(account_id)
    dd = 1.0 - (account.equity or 0.0) / s.fakes.initial_cash
    blocked = await arm.order(d.venue, d.symbol, "buy", "market", round(0.1 * qty, 6), None)
    result.add("drawdown", dd > limits.max_drawdown, blocked)
    harness.platform.exchange.mark_shock.clear()
    harness.clock.advance(limits.order_window_s + 1)
