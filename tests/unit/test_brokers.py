"""Contract 6.8 adapters: the mirror sandbox's matching rules and the T-Bank sandbox over a mocked REST proxy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx

from mcp_gateway.brokers.base import BrokerUnavailable, ConfiguredRules, OrderNotFound
from mcp_gateway.brokers.mirror import BookView, MirrorBroker, TradePrint
from mcp_gateway.brokers.tbank_sandbox import TBankSandboxBroker, from_quotation, quotation, split_symbol
from mcp_gateway.contracts import Actor, OrderRequest
from mcp_gateway.settings import CommissionSettings, InstrumentRule, MirrorSettings, Settings, TBankSandboxSettings
from mcp_gateway.timeutil import NS_PER_SECOND, ns_to_iso

T0 = 1_792_497_600 * NS_PER_SECOND
V, SYM = "moex-tbank", "SBER-RUB-TQBR"


class ScriptedMarket:
    """A market source with one book and a list of trade prints, both settable by the test."""

    def __init__(self) -> None:
        self.book_view: BookView | None = BookView(
            "s1", T0, bids=[(285.00, 100.0), (284.99, 200.0)], asks=[(285.01, 50.0), (285.02, 300.0)]
        )
        self.prints: list[TradePrint] = []

    async def book(self, venue: str, symbol: str, known_at: str) -> BookView | None:
        return self.book_view

    async def trades(self, venue: str, symbol: str, start: str, end: str, known_at: str) -> list[TradePrint]:
        lo, hi = start, end
        return [p for p in self.prints if lo <= ns_to_iso(p.ts_ns) <= hi]


COMMISSION = CommissionSettings(
    source="test",
    currency="RUB",
    rate=0.0025,
    zero_fee_rate=0.0,
    rounding=0.01,
    zero_fee_instruments=["TMOS"],
    zero_fee_reference_file="",
    zero_fee_reference_key="funds",
)


def mirror_cfg(**kw: object) -> MirrorSettings:
    base: dict[str, object] = {
        "initial_cash": 1_000_000.0,
        "currency": "RUB",
        "commission": COMMISSION,
        "book_dataset": "tape.book_snapshot",
        "trades_dataset": "tape.trades",
        "book_lookback_s": 3600.0,
        "max_book_age_s": 600.0,
        "state_file": "mirror.json",
    }
    return MirrorSettings(**(base | kw))  # type: ignore[arg-type]


RULES = ConfiguredRules([InstrumentRule(venue=V, symbol=SYM, lot_size=1, tick=0.01, currency="RUB")])


def broker(market: ScriptedMarket, tmp_path: Path | None = None) -> MirrorBroker:
    state = None if tmp_path is None else tmp_path / "mirror.json"
    return MirrorBroker(market, RULES, mirror_cfg(), state, lambda: T0)


def req(**kw: object) -> OrderRequest:
    base: dict[str, object] = {
        "client_order_id": "c1",
        "account_id": "acc",
        "venue": V,
        "symbol": SYM,
        "side": "buy",
        "type": "market",
        "qty": 10.0,
        "time_in_force": "GTC",
        "strategy_id": "s",
        "actor": Actor(kind="agent", id="a"),
    }
    return OrderRequest(**(base | kw))  # type: ignore[arg-type]


AT = ns_to_iso(T0)


async def test_market_order_walks_the_book_and_pays_taker_fee() -> None:
    m = ScriptedMarket()
    b = broker(m)
    order = await b.place(req(qty=80.0), known_at=AT)
    extra = order.model_extra or {}
    assert order.status == "filled"
    assert extra["avg_price"] == pytest.approx((285.01 * 50 + 285.02 * 30) / 80)
    # 0.25% per fill, each rounded to the kopek: 285.01 * 50 -> 35.63, 285.02 * 30 -> 21.38.
    assert extra["fee"] == pytest.approx(35.63 + 21.38)
    assert [f["fee"] for f in extra["fills"]] == [35.63, 21.38]
    # The same snapshot cannot give its 50 shares at 285.01 twice.
    again = await b.place(req(client_order_id="c2", qty=10.0), known_at=AT)
    assert (again.model_extra or {})["avg_price"] == pytest.approx(285.02)
    (pos,) = await b.positions("acc", known_at=AT)
    assert pos.qty == 90
    assert pos.mark_price == pytest.approx(285.005)
    acct = await b.account("acc", known_at=AT)
    assert acct.balance is not None
    assert acct.balance < 1_000_000 - 90 * 285
    assert acct.equity == pytest.approx(acct.balance + 90 * 285.005)


async def test_market_order_beyond_liquidity_is_cut_and_ioc_rest_cancelled() -> None:
    b = broker(ScriptedMarket())
    order = await b.place(req(qty=1000.0), known_at=AT)
    extra = order.model_extra or {}
    assert order.status == "cancelled"
    assert order.reject_reason == "insufficient_liquidity"
    assert extra["filled_qty"] == 350.0
    ioc = await b.place(req(client_order_id="c2", type="limit", price=285.02, time_in_force="IOC"), known_at=AT)
    assert ioc.status == "cancelled"
    assert (ioc.model_extra or {})["filled_qty"] == 0.0


async def test_fok_post_only_lots_ticks_and_missing_book() -> None:
    m = ScriptedMarket()
    b = broker(m)
    fok = await b.place(req(type="limit", price=285.01, qty=60.0, time_in_force="FOK"), known_at=AT)
    assert (fok.status, fok.reject_reason) == ("rejected", "fok_not_fillable")
    post = await b.place(req(type="limit", price=285.01, time_in_force="POST_ONLY"), known_at=AT)
    assert post.reject_reason == "post_only_would_trade"
    assert (await b.place(req(qty=1.5), known_at=AT)).reject_reason == "qty_not_whole_lots"
    assert (await b.place(req(type="limit", price=285.005), known_at=AT)).reject_reason == "price_off_tick"
    assert (await b.place(req(symbol="GAZP-RUB-TQBR"), known_at=AT)).reject_reason == "unknown_instrument"
    m.book_view = None
    assert (await b.place(req(), known_at=AT)).reject_reason == "no_market_data"


async def test_resting_order_waits_for_its_queue_then_fills_as_maker(tmp_path: Path) -> None:
    m = ScriptedMarket()
    b = broker(m, tmp_path)
    rest = await b.place(req(type="limit", price=285.00, qty=30.0), known_at=AT)
    assert rest.status == "accepted"  # 100 shares are ahead in the queue at 285.00
    m.prints = [TradePrint(T0 + NS_PER_SECOND, 285.00, 80.0, "sell")]
    later = ns_to_iso(T0 + 2 * NS_PER_SECOND)
    assert (await b.get(rest.order_id, known_at=later)).status == "accepted"  # 20 shares still ahead
    m.prints.append(TradePrint(T0 + 3 * NS_PER_SECOND, 285.00, 30.0, "sell"))
    part = await b.get(rest.order_id, known_at=ns_to_iso(T0 + 4 * NS_PER_SECOND))
    assert part.status == "partially_filled"
    assert (part.model_extra or {})["filled_qty"] == 10.0
    m.prints.append(TradePrint(T0 + 5 * NS_PER_SECOND, 284.98, 1.0, "sell"))  # traded through the price
    done = await b.get(rest.order_id, known_at=ns_to_iso(T0 + 6 * NS_PER_SECOND))
    assert done.status == "filled"
    assert (done.model_extra or {})["fee"] == pytest.approx(7.13 + 14.25)  # maker fills of 10 and 20
    # State survives a restart of the gateway.
    again = broker(m, tmp_path)
    assert (await again.get(rest.order_id, known_at=ns_to_iso(T0 + 7 * NS_PER_SECOND))).status == "filled"
    (pos,) = await again.positions("acc", known_at=AT)
    assert pos.qty == 30.0
    assert pos.avg_price == pytest.approx(285.0)


async def test_cancel_and_unknown_order() -> None:
    b = broker(ScriptedMarket())
    rest = await b.place(req(type="limit", price=284.50), known_at=AT)
    assert (await b.cancel(rest.order_id, known_at=AT)).status == "cancelled"
    assert (await b.cancel(rest.order_id, known_at=AT)).status == "cancelled"  # idempotent
    with pytest.raises(OrderNotFound):
        await b.get("nope")


async def test_selling_closes_and_flips_the_position() -> None:
    b = broker(ScriptedMarket())
    await b.place(req(qty=40.0), known_at=AT)
    await b.place(req(client_order_id="c2", side="sell", qty=100.0), known_at=AT)
    (pos,) = await b.positions("acc", known_at=AT)
    assert pos.qty == -60.0
    assert pos.avg_price == pytest.approx(285.0, abs=0.01)


# -- T-Bank sandbox ---------------------------------------------------------------------------------------

BASE = "https://sandbox.example/rest/"
PREFIX = "tinkoff.public.invest.api.contract.v1"


def tbank(tmp_path: Path) -> TBankSandboxBroker:
    cfg = TBankSandboxSettings(
        base_url=BASE,
        service_prefix=PREFIX,
        token_env="TBANK_TOKEN",
        timeout_s=5,
        initial_pay_in=1_000_000,
        currency="RUB",
        state_file="tb.json",
    )
    return TBankSandboxBroker(cfg, tmp_path / "tb.json")


def url(method: str) -> str:
    return f"{BASE}{PREFIX}.{method}"


def test_tbank_helpers() -> None:
    assert quotation(285.37) == {"units": "285", "nano": 370000000}
    assert from_quotation({"units": "-1", "nano": -500000000}) == pytest.approx(-1.5)
    assert split_symbol("SBER-RUB-TQBR") == ("SBER", "TQBR")
    with pytest.raises(BrokerUnavailable):
        split_symbol("BTCUSDT")


async def test_tbank_needs_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TBANK_TOKEN", raising=False)
    with pytest.raises(BrokerUnavailable, match="TBANK_TOKEN"):
        await tbank(tmp_path).instrument(V, SYM)


async def test_tbank_order_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TBANK_TOKEN", "tok")
    share = {
        "instrument": {
            "uid": "u-1",
            "figi": "F1",
            "lot": 10,
            "minPriceIncrement": {"units": "0", "nano": 10000000},
            "currency": "rub",
            "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
        }
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.post(url("InstrumentsService/ShareBy")).respond(200, json=share)
        opened = mock.post(url("SandboxService/OpenSandboxAccount")).respond(200, json={"accountId": "sb-1"})
        payin = mock.post(url("SandboxService/SandboxPayIn")).respond(200, json={"balance": {}})
        posted = mock.post(url("SandboxService/PostSandboxOrder")).respond(
            200,
            json={
                "orderId": "c1",
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsRequested": "3",
                "lotsExecuted": "3",
                "executedOrderPrice": {"units": "8550", "nano": 0},
                "executedCommission": {"units": "4", "nano": 275000000},
            },
        )
        mock.post(url("SandboxService/GetSandboxOrderState")).respond(
            200,
            json={"orderId": "c1", "executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED", "lotsRequested": "3"},
        )
        mock.post(url("SandboxService/CancelSandboxOrder")).respond(200, json={})
        mock.post(url("SandboxService/GetSandboxPortfolio")).respond(
            200,
            json={
                "totalAmountPortfolio": {"units": "1000000"},
                "totalAmountCurrencies": {"units": "991445"},
                "positions": [
                    {"instrumentType": "currency", "quantity": {"units": "991445"}},
                    {
                        "instrumentType": "share",
                        "instrumentUid": "u-1",
                        "quantity": {"units": "30"},
                        "averagePositionPrice": {"units": "285"},
                        "currentPrice": {"units": "286"},
                    },
                ],
            },
        )
        b = tbank(tmp_path)
        rules = await b.instrument(V, SYM)
        assert rules is not None
        assert (rules.lot_size, rules.tick, rules.tradable) == (10.0, 0.01, True)
        order = await b.place(req(qty=30.0), known_at=None)
        body = json.loads(posted.calls[0].request.content)
        assert body["quantity"] == "3"
        assert body["instrumentId"] == "u-1"
        assert body["orderId"] == "c1"
        assert body["orderType"] == "ORDER_TYPE_MARKET"
        assert body["accountId"] == "sb-1"
        assert posted.calls[0].request.headers["Authorization"] == "Bearer tok"
        assert order.status == "filled"
        assert (order.model_extra or {})["filled_qty"] == 30.0
        assert (order.model_extra or {})["avg_price"] == pytest.approx(285.0)
        assert opened.call_count == 1
        assert payin.call_count == 1
        assert (await b.cancel("c1")).status == "cancelled"
        (pos,) = await b.positions("acc", known_at=None)
        assert (pos.symbol, pos.qty, pos.mark_price) == (SYM, 30.0, 286.0)
        acct = await b.account("acc", known_at=None)
        assert acct.equity == 1_000_000.0
        await b.aclose()
    # The account map survives a restart: no second OpenSandboxAccount.
    assert json.loads((tmp_path / "tb.json").read_text())["accounts"] == {"acc": "sb-1"}
    with pytest.raises(OrderNotFound):
        await tbank(tmp_path).get("unknown")


def test_router_rejects_unknown_broker(settings: Settings) -> None:
    from mcp_gateway.brokers.router import BrokerRouter, UnknownBroker
    from mcp_gateway.downstream import build_downstream

    bad = settings.brokers.model_copy(update={"paper": "nowhere"})
    with pytest.raises(UnknownBroker):
        BrokerRouter(bad, build_downstream(settings.services, {}), None, lambda: 0)
    router = BrokerRouter(settings.brokers, build_downstream(settings.services, {}), None, lambda: 0)
    from mcp_gateway.settings import Mode

    assert router.check_choice(Mode.PAPER, None) == "mirror"
    enabled = settings.brokers.model_copy(
        update={
            "allowed_paper": [*settings.brokers.allowed_paper, "tbank-sandbox"],
            "tbank_sandbox": settings.brokers.tbank_sandbox.model_copy(update={"enabled": True}),
        }
    )
    opted_in = BrokerRouter(enabled, build_downstream(settings.services, {}), None, lambda: 0)
    assert opted_in.check_choice(Mode.PAPER, "tbank-sandbox") == "tbank-sandbox"
    with pytest.raises(UnknownBroker):
        router.check_choice(Mode.PAPER, "execution")
    with pytest.raises(UnknownBroker):
        router.check_choice(Mode.LIVE, "mirror")
    with pytest.raises(UnknownBroker):
        router.get("nope")


# -- commission in the mirror (T-Invest tariff) ------------------------------------------------------------

TMOS = "TMOS-RUB-TQTF"


async def test_both_sides_pay_commission_and_pnl_is_net_of_fees() -> None:
    m = ScriptedMarket()
    b = broker(m)
    buy = await b.place(req(qty=50.0), known_at=AT)  # 50 at 285.01
    sell = await b.place(req(client_order_id="c2", side="sell", qty=50.0), known_at=AT)  # 50 at 285.00
    buy_fee, sell_fee = (buy.model_extra or {})["fee"], (sell.model_extra or {})["fee"]
    assert (buy_fee, sell_fee) == (35.63, 35.63)  # 14,250.50 and 14,250.00 roubles, 0.25%, half-up to the kopek
    (fill,) = (sell.model_extra or {})["fills"]
    assert fill == {"ts_fill": AT, "price": 285.0, "qty": 50.0, "fee": 35.63, "liquidity": "taker"}
    acct = await b.account("acc", known_at=AT)
    extra = acct.model_extra or {}
    assert extra["fees"] == pytest.approx(71.26)
    assert acct.balance == pytest.approx(1_000_000 - 50 * 0.01 - 71.26)
    assert extra["pnl"] == pytest.approx(-50 * 0.01 - 71.26)  # flat again: spread paid plus both fees


async def test_zero_fee_fund_pays_nothing() -> None:
    rules = ConfiguredRules(
        [
            InstrumentRule(venue=V, symbol=SYM, lot_size=1, tick=0.01, currency="RUB"),
            InstrumentRule(venue=V, symbol=TMOS, lot_size=1, tick=0.01, currency="RUB"),
        ]
    )
    b = MirrorBroker(ScriptedMarket(), rules, mirror_cfg(), None, lambda: T0)
    fund = await b.place(req(symbol=TMOS, qty=100.0), known_at=AT)
    stock = await b.place(req(qty=100.0), known_at=AT)
    assert (fund.model_extra or {})["filled_qty"] == 100.0
    assert (fund.model_extra or {})["fee"] == 0.0
    assert (stock.model_extra or {})["fee"] > 0
    acct = await b.account("acc", known_at=AT)
    assert (acct.model_extra or {})["fees"] == (stock.model_extra or {})["fee"]


async def test_mirror_state_without_deposit_field_still_loads(tmp_path: Path) -> None:
    state = tmp_path / "mirror.json"
    state.write_text(json.dumps({"accounts": {"acc": {"cash": 1_000_000.0, "currency": "RUB", "fees": 0.0}}}))
    acct = await broker(ScriptedMarket(), tmp_path).account("acc", known_at=AT)
    assert (acct.model_extra or {})["pnl"] == 0.0


def test_tbank_sandbox_is_disabled_by_default(settings: Settings) -> None:
    from mcp_gateway.brokers.router import BrokerDisabled, BrokerRouter
    from mcp_gateway.downstream import build_downstream
    from mcp_gateway.settings import Mode

    cfg = settings.brokers
    assert cfg.tbank_sandbox.enabled is False
    assert "tbank-sandbox" not in (cfg.paper, cfg.live, *cfg.allowed_paper)
    downstream = build_downstream(settings.services, {})
    router = BrokerRouter(cfg, downstream, None, lambda: 0)
    with pytest.raises(BrokerDisabled, match="enabled is false"):
        router.check_choice(Mode.PAPER, "tbank-sandbox")
    with pytest.raises(BrokerDisabled):
        router.get("tbank-sandbox")
    # Even when listed in allowed_paper, the flag still wins.
    listed = BrokerRouter(
        cfg.model_copy(update={"allowed_paper": [*cfg.allowed_paper, "tbank-sandbox"]}), downstream, None, lambda: 0
    )
    with pytest.raises(BrokerDisabled):
        listed.check_choice(Mode.PAPER, "tbank-sandbox")
    # A default that points at T-Bank while disabled stops the gateway from starting.
    with pytest.raises(BrokerDisabled):
        BrokerRouter(cfg.model_copy(update={"paper": "tbank-sandbox"}), downstream, None, lambda: 0)
