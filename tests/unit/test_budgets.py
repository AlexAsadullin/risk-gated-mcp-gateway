import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_gateway import budgets as bud
from mcp_gateway.sessions import Usage
from mcp_gateway.settings import BreachAction, BudgetKind, BudgetLimits, BudgetOverrides, ModelPrice

LIMITS = BudgetLimits(
    capital=1000.0,
    max_drawdown=0.1,
    max_order_notional=100.0,
    max_orders_per_window=2,
    order_window_s=60.0,
    max_tokens=1000,
    max_token_cost_usd=1.0,
    max_latency_ms=500.0,
    max_total_latency_ms=1000.0,
)


def test_token_cost_closed_form() -> None:
    price = ModelPrice(input_per_mtok_usd=3.0, output_per_mtok_usd=15.0)
    assert bud.token_cost_usd(price, 1_000_000, 0) == pytest.approx(3.0)
    assert bud.token_cost_usd(price, 1000, 2000) == pytest.approx(0.003 + 0.03)


def test_drawdown() -> None:
    assert bud.drawdown(100.0, 90.0) == pytest.approx(0.1)
    assert bud.drawdown(100.0, 110.0) == 0.0
    assert bud.drawdown(0.0, 10.0) == 0.0


@given(st.floats(1.0, 1e9), st.floats(0.0, 1e9))
def test_drawdown_bounds(peak: float, equity: float) -> None:
    assert 0.0 <= bud.drawdown(peak, equity) <= 1.0


def test_order_window() -> None:
    times = [0, 30 * 10**9, 90 * 10**9]
    assert bud.orders_in_window(times, 100 * 10**9, 60.0) == 1
    assert bud.prune_order_times(times, 100 * 10**9, 60.0) == [90 * 10**9]


def test_usage_breaches_each_kind() -> None:
    usage = Usage(tokens_in=900, tokens_out=200, token_cost_usd=2.0, latency_total_ms=2000.0)
    kinds = {b.kind for b in bud.usage_breaches(LIMITS, usage, 600.0)}
    assert kinds == {BudgetKind.TOKENS, BudgetKind.TOKEN_COST, BudgetKind.LATENCY, BudgetKind.TOTAL_LATENCY}
    assert bud.usage_breaches(LIMITS, Usage(), 1.0) == []


def test_order_breaches() -> None:
    usage = Usage(order_times_ns=[10**9, 2 * 10**9])
    kinds = {
        b.kind
        for b in bud.order_breaches(LIMITS, usage, order_notional=150.0, gross_exposure_after=2000.0, now_ns=3 * 10**9)
    }
    assert kinds == {BudgetKind.ORDER_NOTIONAL, BudgetKind.CAPITAL, BudgetKind.ORDER_RATE}
    assert bud.order_breaches(LIMITS, Usage(), order_notional=50.0, gross_exposure_after=50.0, now_ns=0) == []


def test_drawdown_breach_and_freeze_policy() -> None:
    assert bud.drawdown_breach(LIMITS, 100.0, 95.0) is None
    breach = bud.drawdown_breach(LIMITS, 100.0, 80.0)
    assert breach is not None and breach.kind is BudgetKind.DRAWDOWN
    policy = dict.fromkeys(BudgetKind, BreachAction.REFUSE) | {BudgetKind.DRAWDOWN: BreachAction.FREEZE}
    assert bud.breach_freezes([breach], policy)
    rate = bud.Breach(kind=BudgetKind.ORDER_RATE, value=3, limit=2, message="x")
    assert not bud.breach_freezes([rate], policy)


def test_remaining_never_negative() -> None:
    usage = Usage(tokens_in=5000, token_cost_usd=3.0, peak_equity=100.0, last_equity=50.0)
    rem = bud.remaining(LIMITS, usage, 0)
    assert all(v >= 0 for v in rem.values())
    assert rem["tokens"] == 0.0


def test_budget_overrides_apply() -> None:
    merged = BudgetOverrides(max_tokens=5).apply(LIMITS)
    assert merged.max_tokens == 5 and merged.capital == LIMITS.capital
