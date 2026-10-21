"""Pre-trade checks applied to every order before it is forwarded to I6 or I7.

Pure functions over the order intent, a reference price and current positions. Notional is computed in the
quote currency as ``qty * px`` with ``px`` the limit price for limit orders and the reference price for market
orders; float64 is adequate here because limits are coarse thresholds, not accounting balances.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from mcp_gateway.contracts import OrderType, Position, Side
from mcp_gateway.settings import RiskSettings

BPS = 10_000.0


@dataclass(frozen=True)
class OrderIntent:
    venue: str
    symbol: str
    side: Side
    type: OrderType
    qty: float
    price: float | None


class CheckResult(BaseModel):
    name: str
    passed: bool
    value: float | str | None = None
    threshold: float | str | None = None


def signed_qty(side: Side, qty: float) -> float:
    return qty if side == "buy" else -qty


def execution_price(intent: OrderIntent, reference_price: float | None) -> float | None:
    """Price used to value the order: the limit price, or the reference price for market orders."""
    if intent.type == "limit":
        return intent.price
    return reference_price


def order_notional(intent: OrderIntent, reference_price: float | None) -> float | None:
    px = execution_price(intent, reference_price)
    return None if px is None else abs(intent.qty) * px


def position_value_price(position: Position, fallback: float | None) -> float | None:
    """Best available valuation price for an existing position: mark, then average, then implied, then fallback."""
    if position.mark_price is not None:
        return position.mark_price
    if position.avg_price is not None:
        return position.avg_price
    if position.notional is not None and position.qty != 0:
        return abs(position.notional / position.qty)
    return fallback


def current_qty(positions: list[Position], venue: str, symbol: str) -> float:
    return sum(p.qty for p in positions if p.venue == venue and p.symbol == symbol)


def gross_exposure_after(positions: list[Position], intent: OrderIntent, reference_price: float | None) -> float:
    """Sum of ``|qty| * px`` over all positions after the order fills in full."""
    gross = 0.0
    for position in positions:
        if position.venue == intent.venue and position.symbol == intent.symbol:
            continue
        px = position_value_price(position, None)
        gross += abs(position.qty) * (px or 0.0)
    px = execution_price(intent, reference_price) or 0.0
    after = current_qty(positions, intent.venue, intent.symbol) + signed_qty(intent.side, intent.qty)
    return gross + abs(after) * px


def pre_trade_checks(
    intent: OrderIntent,
    *,
    reference_price: float | None,
    positions: list[Position],
    risk: RiskSettings,
) -> list[CheckResult]:
    """Run every pre-trade check and return all results (failed and passed) for the audit log."""
    whitelisted = any(i.venue == intent.venue and i.symbol == intent.symbol for i in risk.whitelist)
    checks = [
        CheckResult(
            name="instrument_whitelist",
            passed=whitelisted,
            value=f"{intent.venue}:{intent.symbol}",
            threshold="config risk.whitelist",
        ),
        CheckResult(name="qty_positive", passed=intent.qty > 0, value=intent.qty, threshold=0.0),
    ]
    if intent.type == "limit":
        has_price = intent.price is not None and intent.price > 0
        checks.append(CheckResult(name="limit_price_present", passed=has_price, value=intent.price))
    has_ref = reference_price is not None and reference_price > 0
    checks.append(
        CheckResult(
            name="reference_price",
            passed=has_ref or not risk.require_reference_price,
            value=reference_price,
            threshold="required" if risk.require_reference_price else "optional",
        )
    )
    if intent.type == "limit" and intent.price is not None and reference_price:
        deviation = abs(intent.price / reference_price - 1.0) * BPS
        checks.append(
            CheckResult(
                name="price_band_bps",
                passed=deviation <= risk.price_band_bps,
                value=round(deviation, 4),
                threshold=risk.price_band_bps,
            )
        )
    notional = order_notional(intent, reference_price)
    checks.append(
        CheckResult(
            name="order_notional",
            passed=notional is not None and notional <= risk.max_order_notional,
            value=notional,
            threshold=risk.max_order_notional,
        )
    )
    px = execution_price(intent, reference_price)
    if px is not None:
        after = current_qty(positions, intent.venue, intent.symbol) + signed_qty(intent.side, intent.qty)
        position_notional = abs(after) * px
        checks.append(
            CheckResult(
                name="position_limit",
                passed=position_notional <= risk.max_position_notional,
                value=position_notional,
                threshold=risk.max_position_notional,
            )
        )
    return checks


def failed(checks: list[CheckResult]) -> list[CheckResult]:
    return [c for c in checks if not c.passed]
