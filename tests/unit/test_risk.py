from mcp_gateway.brokers.base import InstrumentRules
from mcp_gateway.contracts import Position
from mcp_gateway.risk import (
    OrderIntent,
    failed,
    gross_exposure_after,
    instrument_checks,
    on_grid,
    order_notional,
    position_value_price,
    pre_trade_checks,
)
from mcp_gateway.settings import Instrument, ReferencePriceSettings, RiskSettings

RISK = RiskSettings(
    whitelist=[Instrument(venue="moex-tbank", symbol="SBER-RUB-TQBR")],
    max_order_notional=1000.0,
    max_position_notional=1500.0,
    price_band_bps=100.0,
    require_reference_price=True,
    require_instrument_rules=True,
    reference=ReferencePriceSettings(channel="trades", lookback_s=3600, price_column="price"),
)


def names(checks: list) -> set[str]:  # type: ignore[type-arg]
    return {c.name for c in failed(checks)}


def intent(**kw: object) -> OrderIntent:
    base: dict[str, object] = {
        "venue": "moex-tbank",
        "symbol": "SBER-RUB-TQBR",
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
        pre_trade_checks(intent(venue="moex-algopack"), reference_price=500.0, positions=[], risk=RISK)
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
    held = [Position(venue="moex-tbank", symbol="SBER-RUB-TQBR", qty=2.5, mark_price=500.0)]
    assert names(pre_trade_checks(intent(), reference_price=500.0, positions=held, risk=RISK)) == {"position_limit"}


def test_band_inside_and_sell_reduces_position() -> None:
    ok = pre_trade_checks(intent(type="limit", price=504.0), reference_price=500.0, positions=[], risk=RISK)
    assert failed(ok) == []
    held = [Position(venue="moex-tbank", symbol="SBER-RUB-TQBR", qty=2.5, mark_price=500.0)]
    assert failed(pre_trade_checks(intent(side="sell"), reference_price=500.0, positions=held, risk=RISK)) == []


def test_exposure_and_valuation() -> None:
    other = Position(venue="moex-tbank", symbol="GAZP-RUB-TQBR", qty=-2.0, avg_price=100.0)
    implied = Position(venue="moex-tbank", symbol="LKOH-RUB-TQBR", qty=4.0, notional=40.0)
    bare = Position(venue="moex-tbank", symbol="X", qty=1.0)
    assert position_value_price(implied, None) == 10.0
    assert position_value_price(bare, 7.0) == 7.0
    gross = gross_exposure_after([other, implied, bare], intent(), 500.0)
    assert gross == 200.0 + 40.0 + 0.0 + 500.0
    assert order_notional(intent(type="limit", price=10.0), 500.0) == 10.0
    assert order_notional(intent(), None) is None


LKOH = InstrumentRules(venue="moex-tbank", symbol="LKOH-RUB-TQBR", lot_size=1, tick=0.5, currency="RUB")
GAZP = InstrumentRules(
    venue="moex-tbank", symbol="GAZP-RUB-TQBR", lot_size=10, tick=0.01, currency="RUB", price_band_bps=2000
)


def test_on_grid_tolerates_float_rounding() -> None:
    assert on_grid(0.3, 0.1)
    assert on_grid(285.37, 0.01)
    assert not on_grid(285.375, 0.01)
    assert on_grid(30.0, 10.0)
    assert not on_grid(15.0, 10.0)


def test_lots_ticks_band_and_status() -> None:
    ok = intent(symbol="GAZP-RUB-TQBR", type="limit", qty=20.0, price=130.01)
    assert names(instrument_checks(ok, GAZP, 130.0, required=True)) == set()
    odd_lot = intent(symbol="GAZP-RUB-TQBR", qty=15.0)
    assert names(instrument_checks(odd_lot, GAZP, 130.0, required=True)) == {"qty_whole_lots"}
    off_tick = intent(symbol="LKOH-RUB-TQBR", type="limit", qty=1.0, price=6900.2)
    assert names(instrument_checks(off_tick, LKOH, 6900.0, required=True)) == {"price_on_tick"}
    outside = intent(symbol="GAZP-RUB-TQBR", type="limit", qty=10.0, price=170.0)
    assert names(instrument_checks(outside, GAZP, 130.0, required=True)) == {"exchange_price_band_bps"}
    halted = GAZP.model_copy(update={"tradable": False, "trading_status": "BREAK_IN_TRADING"})
    assert names(instrument_checks(intent(qty=10.0), halted, 130.0, required=True)) == {"instrument_tradable"}


def test_missing_rules_fail_only_when_required() -> None:
    assert names(instrument_checks(intent(), None, 1.0, required=True)) == {"instrument_rules"}
    assert names(instrument_checks(intent(), None, 1.0, required=False)) == set()
