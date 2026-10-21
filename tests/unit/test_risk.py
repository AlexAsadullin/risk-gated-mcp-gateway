from mcp_gateway.contracts import Position
from mcp_gateway.risk import (
    OrderIntent,
    failed,
    gross_exposure_after,
    order_notional,
    position_value_price,
    pre_trade_checks,
)
from mcp_gateway.settings import Instrument, ReferencePriceSettings, RiskSettings

RISK = RiskSettings(
    whitelist=[Instrument(venue="binance", symbol="BTC-USDT-PERP")],
    max_order_notional=1000.0,
    max_position_notional=1500.0,
    price_band_bps=100.0,
    require_reference_price=True,
    reference=ReferencePriceSettings(channel="trades", lookback_s=3600, price_column="price"),
)


def names(checks: list) -> set[str]:  # type: ignore[type-arg]
    return {c.name for c in failed(checks)}


def intent(**kw: object) -> OrderIntent:
    base: dict[str, object] = {
        "venue": "binance",
        "symbol": "BTC-USDT-PERP",
        "side": "buy",
        "type": "market",
        "qty": 1.0,
        "price": None,
    }
    return OrderIntent(**(base | kw))  # type: ignore[arg-type]


def test_clean_order_passes() -> None:
    assert failed(pre_trade_checks(intent(), reference_price=500.0, positions=[], risk=RISK)) == []


def test_each_check_can_fail() -> None:
    assert "instrument_whitelist" in names(
        pre_trade_checks(intent(venue="bybit"), reference_price=500.0, positions=[], risk=RISK)
    )
    assert "qty_positive" in names(pre_trade_checks(intent(qty=-1.0), reference_price=500.0, positions=[], risk=RISK))
    assert "limit_price_present" in names(
        pre_trade_checks(intent(type="limit"), reference_price=500.0, positions=[], risk=RISK)
    )
    assert "reference_price" in names(pre_trade_checks(intent(), reference_price=None, positions=[], risk=RISK))
    assert "price_band_bps" in names(
        pre_trade_checks(intent(type="limit", price=520.0), reference_price=500.0, positions=[], risk=RISK)
    )
    assert "order_notional" in names(pre_trade_checks(intent(qty=3.0), reference_price=500.0, positions=[], risk=RISK))
    held = [Position(venue="binance", symbol="BTC-USDT-PERP", qty=2.5, mark_price=500.0)]
    assert names(pre_trade_checks(intent(), reference_price=500.0, positions=held, risk=RISK)) == {"position_limit"}


def test_band_inside_and_sell_reduces_position() -> None:
    ok = pre_trade_checks(intent(type="limit", price=504.0), reference_price=500.0, positions=[], risk=RISK)
    assert failed(ok) == []
    held = [Position(venue="binance", symbol="BTC-USDT-PERP", qty=2.5, mark_price=500.0)]
    assert failed(pre_trade_checks(intent(side="sell"), reference_price=500.0, positions=held, risk=RISK)) == []


def test_exposure_and_valuation() -> None:
    other = Position(venue="binance", symbol="ETH-USDT-PERP", qty=-2.0, avg_price=100.0)
    implied = Position(venue="binance", symbol="SOL-USDT-PERP", qty=4.0, notional=40.0)
    bare = Position(venue="binance", symbol="X", qty=1.0)
    assert position_value_price(implied, None) == 10.0
    assert position_value_price(bare, 7.0) == 7.0
    gross = gross_exposure_after([other, implied, bare], intent(), 500.0)
    assert gross == 200.0 + 40.0 + 0.0 + 500.0
    assert order_notional(intent(type="limit", price=10.0), 500.0) == 10.0
    assert order_notional(intent(), None) is None
