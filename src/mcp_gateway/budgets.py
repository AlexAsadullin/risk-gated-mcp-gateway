"""Per-agent budget arithmetic. Pure functions; the gateway decides what a breach does (refuse or freeze)."""

from __future__ import annotations

from pydantic import BaseModel

from mcp_gateway.sessions import Usage
from mcp_gateway.settings import BreachAction, BudgetKind, BudgetLimits, ModelPrice
from mcp_gateway.timeutil import NS_PER_SECOND

TOKENS_PER_MTOK = 1_000_000


class Breach(BaseModel):
    kind: BudgetKind
    value: float
    limit: float
    message: str


def token_cost_usd(price: ModelPrice, tokens_in: int, tokens_out: int) -> float:
    """Cost in USD: ``tokens_in * p_in / 1e6 + tokens_out * p_out / 1e6`` (prices are per million tokens)."""
    return (tokens_in * price.input_per_mtok_usd + tokens_out * price.output_per_mtok_usd) / TOKENS_PER_MTOK


def drawdown(peak_equity: float, equity: float) -> float:
    """Fractional drawdown from the running peak, ``max(0, 1 - equity / peak)``; 0 when peak is not positive."""
    if peak_equity <= 0:
        return 0.0
    return max(0.0, 1.0 - equity / peak_equity)


def orders_in_window(order_times_ns: list[int], now_ns: int, window_s: float) -> int:
    """Number of submitted orders whose timestamp falls in ``(now - window, now]``."""
    cutoff = now_ns - int(window_s * NS_PER_SECOND)
    return sum(1 for ts in order_times_ns if cutoff < ts <= now_ns)


def prune_order_times(order_times_ns: list[int], now_ns: int, window_s: float) -> list[int]:
    cutoff = now_ns - int(window_s * NS_PER_SECOND)
    return [ts for ts in order_times_ns if ts > cutoff]


def usage_breaches(limits: BudgetLimits, usage: Usage, last_latency_ms: float) -> list[Breach]:
    """Breaches of the intelligence budgets (tokens, spend, latency) after a usage report is applied."""
    breaches: list[Breach] = []
    if usage.tokens_total > limits.max_tokens:
        breaches.append(
            Breach(
                kind=BudgetKind.TOKENS,
                value=usage.tokens_total,
                limit=limits.max_tokens,
                message=f"token budget exhausted: {usage.tokens_total} > {limits.max_tokens} tokens",
            )
        )
    if usage.token_cost_usd > limits.max_token_cost_usd:
        breaches.append(
            Breach(
                kind=BudgetKind.TOKEN_COST,
                value=usage.token_cost_usd,
                limit=limits.max_token_cost_usd,
                message=f"token spend exhausted: ${usage.token_cost_usd:.4f} > ${limits.max_token_cost_usd:.4f}",
            )
        )
    if last_latency_ms > limits.max_latency_ms:
        breaches.append(
            Breach(
                kind=BudgetKind.LATENCY,
                value=last_latency_ms,
                limit=limits.max_latency_ms,
                message=f"decision latency {last_latency_ms:.0f} ms > {limits.max_latency_ms:.0f} ms",
            )
        )
    if usage.latency_total_ms > limits.max_total_latency_ms:
        breaches.append(
            Breach(
                kind=BudgetKind.TOTAL_LATENCY,
                value=usage.latency_total_ms,
                limit=limits.max_total_latency_ms,
                message=(f"cumulative latency {usage.latency_total_ms:.0f} ms > {limits.max_total_latency_ms:.0f} ms"),
            )
        )
    return breaches


def order_breaches(
    limits: BudgetLimits,
    usage: Usage,
    *,
    order_notional: float,
    gross_exposure_after: float,
    now_ns: int,
) -> list[Breach]:
    """Breaches of the capital, order-size and order-rate budgets for a prospective order."""
    breaches: list[Breach] = []
    if order_notional > limits.max_order_notional:
        breaches.append(
            Breach(
                kind=BudgetKind.ORDER_NOTIONAL,
                value=order_notional,
                limit=limits.max_order_notional,
                message=f"order notional {order_notional:.2f} > budget {limits.max_order_notional:.2f}",
            )
        )
    if gross_exposure_after > limits.capital:
        breaches.append(
            Breach(
                kind=BudgetKind.CAPITAL,
                value=gross_exposure_after,
                limit=limits.capital,
                message=f"gross exposure after trade {gross_exposure_after:.2f} > capital {limits.capital:.2f}",
            )
        )
    recent = orders_in_window(usage.order_times_ns, now_ns, limits.order_window_s)
    if recent + 1 > limits.max_orders_per_window:
        breaches.append(
            Breach(
                kind=BudgetKind.ORDER_RATE,
                value=recent + 1,
                limit=limits.max_orders_per_window,
                message=(
                    f"order rate: {recent + 1} orders in {limits.order_window_s:.0f} s > {limits.max_orders_per_window}"
                ),
            )
        )
    return breaches


def drawdown_breach(limits: BudgetLimits, peak_equity: float, equity: float) -> Breach | None:
    dd = drawdown(peak_equity, equity)
    if dd <= limits.max_drawdown:
        return None
    return Breach(
        kind=BudgetKind.DRAWDOWN,
        value=dd,
        limit=limits.max_drawdown,
        message=f"drawdown {dd:.2%} from peak equity {peak_equity:.2f} > {limits.max_drawdown:.2%}",
    )


def breach_freezes(breaches: list[Breach], on_breach: dict[BudgetKind, BreachAction]) -> bool:
    """A set of breaches freezes the session if any of them is configured to freeze."""
    return any(on_breach[b.kind] is BreachAction.FREEZE for b in breaches)


def remaining(limits: BudgetLimits, usage: Usage, now_ns: int) -> dict[str, float]:
    """Headroom left on each cumulative budget (never negative)."""
    peak = usage.peak_equity
    last = usage.last_equity
    dd = drawdown(peak, last) if peak is not None and last is not None else 0.0
    recent = orders_in_window(usage.order_times_ns, now_ns, limits.order_window_s)
    return {
        "tokens": float(max(0, limits.max_tokens - usage.tokens_total)),
        "token_cost_usd": max(0.0, limits.max_token_cost_usd - usage.token_cost_usd),
        "total_latency_ms": max(0.0, limits.max_total_latency_ms - usage.latency_total_ms),
        "drawdown": max(0.0, limits.max_drawdown - dd),
        "orders_in_window": float(max(0, limits.max_orders_per_window - recent)),
    }
