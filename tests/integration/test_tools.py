"""Contract 6.6 end to end through an in-process MCP client against the fakes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from mcp_gateway.harness import AgentConnection, Harness
from mcp_gateway.mcp_server import TOOL_NAMES
from mcp_gateway.settings import Settings, TimeTravelPolicy, UnknownModelPolicy
from mcp_gateway.timeutil import parse_iso, to_iso
from tests.conftest import ADMIN_KEY, with_updates


def window(h: Harness, hours: float = 1.0, after: float = 0.0) -> dict[str, str]:
    known = parse_iso(h.settings.demo.known_at)
    d = h.settings.demo
    return {
        "venue": d.venue,
        "symbol": d.symbol,
        "channel": "trades",
        "start": to_iso(known - timedelta(hours=hours)),
        "end": to_iso(known + timedelta(hours=after)),
    }


def order(h: Harness, **kw: Any) -> dict[str, Any]:
    d = h.settings.demo
    base = {
        "venue": d.venue,
        "symbol": d.symbol,
        "side": "buy",
        "type": "market",
        "qty": 10.0,
        "strategy_id": "mom",
        "rationale": "test",
    }
    return base | kw


async def research(h: Harness) -> dict[str, Any]:
    return await h.create_session(agent_id="ag", mode="research", known_at=h.settings.demo.known_at)


async def paper(h: Harness, **budgets: Any) -> dict[str, Any]:
    """A paper session on the I6 fake, whose bookkeeping these tests inspect; the mirror has its own tests."""
    return await h.create_session(agent_id="ag", mode="paper", known_at=None, budgets=budgets, broker="shadow")


async def test_tool_list_matches_contract(harness: Harness) -> None:
    s = await research(harness)
    async with harness.agent(s["api_key"]) as a:
        tools = (await a.client.list_tools()).tools
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    for tool in tools:
        assert "known_at" not in tool.input_schema.get("properties", {})
        assert tool.output_schema is not None
        assert tool.description
    by_name = {t.name: t for t in tools}
    assert set(by_name["place_order"].input_schema["required"]) >= {
        "venue",
        "symbol",
        "side",
        "type",
        "qty",
        "strategy_id",
        "rationale",
    }
    assert set(by_name["report_usage"].input_schema["properties"]) == {"tokens_in", "tokens_out", "latency_ms", "model"}


async def test_research_flow(harness: Harness) -> None:
    s = await research(harness)
    async with harness.agent(s["api_key"]) as a:
        clock = await a.call("clock")
        assert clock.data["known_at"] == "2025-06-01T00:00:00Z"
        assert clock.data["known_at_source"] == "session"
        data = await a.call("market_data", **window(harness))
        assert not data.is_error
        assert data.data["n_rows"] == 60
        assert data.data["fingerprint"].startswith("sha256:")
        rows = data.data["rows"]
        assert max(parse_iso(r["knowledge_time"]) for r in rows) <= parse_iso("2025-06-01T00:00:00Z")
        assert [r["valid_time"] for r in rows] == sorted(r["valid_time"] for r in rows)
        assert data.data["leakage_flags"] == []
        big = await a.call("market_data", **window(harness, hours=24))
        assert big.data["truncated"]
        assert len(big.data["rows"]) == harness.settings.data.max_rows
        leak = await a.call("market_data", **window(harness, after=1))
        assert leak.code == "time_travel"
        bad = await a.call("market_data", **(window(harness) | {"channel": "nope"}))
        assert bad.code == "invalid_request"
        feats = await a.call(
            "features",
            names=["ret_1h"],
            symbols=["SBER-RUB-TQBR"],
            start=window(harness)["start"],
            end=window(harness)["end"],
        )
        assert not feats.is_error
        assert "ret_1h" in feats.data["rows"][0]
        fc = await a.call("forecast", model="m", symbols=["SBER-RUB-TQBR"], horizon=4)
        assert fc.data["known_at"] == "2025-06-01T00:00:00Z"
        assert set(fc.data["forecasts"][0]["quantiles"]) == {
            "0.1",
            "0.5",
            "0.9",
        }
        assert (await a.call("forecast", model="m", symbols=["X"], horizon=10_000)).code == "invalid_request"
        bt = await a.call(
            "run_backtest",
            strategy="mom",
            params={"seed": 3},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        assert not bt.is_error
        assert bt.data["logged_to"] == "i5"
        assert bt.data["gateway_trial_id"] == bt.data["backtest_trial_id"]
        assert bt.data["ledger_strategy_id"] == "ag:mom"
        peek = await a.call(
            "run_backtest",
            strategy="mom",
            params={},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-02",
            fidelity="event",
        )
        assert peek.code == "time_travel"
        assert (await a.call("place_order", **order(harness))).code == "mode_forbidden"
        assert (await a.call("positions")).code == "mode_forbidden"
        assert (await a.call("cancel_order", order_id="x")).code == "mode_forbidden"
    events = harness.platform.ledger.leakage
    assert [(e.source, e.severity, e.detail["tool"]) for e in events] == [
        ("i9", "block", "market_data"),
        ("i9", "block", "run_backtest"),
    ]
    assert events[0].strategy_id is None
    assert events[1].strategy_id == "ag:mom"
    assert events[0].detail["agent_id"] == "ag"
    assert events[0].kind == "end_after_known_at"
    trials = harness.platform.ledger.trials
    assert len(trials) == 3
    assert trials[0].seed == 3
    assert trials[0].source == "i5"
    assert trials[0].strategy_id == "ag:mom"
    assert trials[0].actor.model_dump() == {"kind": "agent", "id": "ag"}
    assert "refused:time_travel" in trials[1].tags
    assert "refused:mode_forbidden" in trials[2].tags
    assert all(q.mode == "research" and q.known_at == "2025-06-01T00:00:00Z" for q in harness.platform.pit_queries)
    assert harness.platform.backtests[0].known_at == "2025-06-01T00:00:00Z"
    assert len(harness.platform.backtests) == 1


async def test_clamp_policy_and_future_rows_dropped(settings: Settings) -> None:
    s2 = with_updates(settings, "data", time_travel_policy=TimeTravelPolicy.CLAMP)
    h = Harness.create(s2, admin_key=ADMIN_KEY, leak_future_rows=True)
    s = await research(h)
    async with h.agent(s["api_key"]) as a:
        out = await a.call("market_data", **window(h, after=2))
    assert out.data["clamped"]
    assert out.data["end"] == "2025-06-01T00:00:00Z"
    assert out.data["dropped_future_rows"] == 1
    assert [(e.source, e.severity, e.kind) for e in h.platform.ledger.leakage] == [
        ("i9", "warn", "end_clamped_to_known_at")
    ]
    await h.gateway.downstream.aclose()


async def test_pit_leakage_guard_maps_to_refusal(settings: Settings) -> None:
    def conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"source": "i2", "kind": "no_knowledge_time"})

    h = Harness.create(settings, admin_key=ADMIN_KEY, overrides={"pit": httpx.MockTransport(conflict)})
    s = await research(h)
    async with h.agent(s["api_key"]) as a:
        out = await a.call("market_data", **window(h))
    assert out.code == "time_travel"
    assert out.data["details"]["enforced_by"] == "i2"
    await h.gateway.downstream.aclose()


async def test_paper_orders_checks_and_budgets(harness: Harness) -> None:
    s = await paper(harness, max_orders_per_window=2)
    async with harness.agent(s["api_key"]) as a:
        ok = await a.call("place_order", **order(harness))
        assert ok.data["routed_to"] == "i6"
        assert ok.data["order"]["status"] == "filled"
        ref = ok.data["reference_price"]
        fat = await a.call("place_order", **order(harness, type="limit", price=round(ref * 1.05, 2)))
        assert fat.code == "pre_trade_rejected"
        failed = {c["name"] for c in fat.data["details"]["checks"] if not c["passed"]}
        assert failed == {"price_band_bps"}
        assert (await a.call("place_order", **order(harness, type="limit"))).code == "pre_trade_rejected"
        assert (await a.call("place_order", **order(harness, venue="kraken"))).code == "pre_trade_rejected"
        big = await a.call("place_order", **order(harness, qty=5000.0))
        assert big.code == "budget_breached"
        assert big.data["session_status"] == "active"
        rest = await a.call("place_order", **order(harness, type="limit", price=round(ref * 0.995, 2)))
        assert rest.data["order"]["status"] == "accepted"
        cancel = await a.call("cancel_order", order_id=rest.data["order"]["order_id"])
        assert cancel.data["order"]["status"] == "cancelled"
        assert (await a.call("cancel_order", order_id="ord-99999")).code == "not_found"
        rate = await a.call("place_order", **order(harness))
        assert rate.code == "budget_breached"
        assert rate.data["details"]["breaches"][0]["kind"] == "order_rate"
        harness.clock.advance(61)
        assert not (await a.call("place_order", **order(harness))).is_error
        pos = await a.call("positions")
        assert pos.data["positions"][0]["qty"] == pytest.approx(20.0)
        acct = await a.call("account")
        assert acct.data["drawdown"] == pytest.approx(0.0, abs=1e-3)
        budget = await a.call("budget")
        assert budget.data["usage"]["orders_submitted"] == 3
        assert budget.data["status"] == "active"
    intents = [t for t in harness.platform.ledger.trials if "order-intent" in t.tags]
    assert len(intents) == 8
    assert len(harness.platform.exchange.received) == 3


async def test_cancel_other_account_is_not_found(harness: Harness) -> None:
    first = await harness.create_session(agent_id="one", mode="paper", broker="shadow")
    second = await harness.create_session(agent_id="two", mode="paper", broker="shadow")
    async with harness.agent(first["api_key"]) as a:
        ref = (await a.call("place_order", **order(harness))).data["reference_price"]
        rest = await a.call("place_order", **order(harness, type="limit", price=round(ref * 0.995, 2)))
    async with harness.agent(second["api_key"]) as b:
        out = await b.call("cancel_order", order_id=rest.data["order"]["order_id"])
    assert out.code == "not_found"


async def test_capital_budget(harness: Harness) -> None:
    s = await paper(harness, capital=100.0)
    async with harness.agent(s["api_key"]) as a:
        out = await a.call("place_order", **order(harness))
    kinds = {b["kind"] for b in out.data["details"]["breaches"]}
    assert kinds == {"capital"}


async def test_drawdown_freezes(harness: Harness) -> None:
    s = await paper(harness)
    async with harness.agent(s["api_key"]) as a:
        for _ in range(4):
            assert not (await a.call("place_order", **order(harness, qty=3400.0))).is_error
        harness.platform.exchange.mark_shock[harness.settings.demo.symbol] = 0.7
        out = await a.call("account")
        assert out.code == "budget_breached"
        assert out.data["session_status"] == "frozen"
        again = await a.call("account")
        assert not again.is_error
        assert again.data["drawdown"] > 0.1
        assert (await a.call("place_order", **order(harness))).code == "session_frozen"
        assert (await a.call("budget")).data["freeze"]["budget"] == "drawdown"


async def test_usage_budgets_and_pricing(harness: Harness) -> None:
    s = await paper(harness, max_latency_ms=100.0)
    async with harness.agent(s["api_key"]) as a:
        ok = await a.call("report_usage", tokens_in=500_000, tokens_out=0, latency_ms=10.0, model="claude-haiku-4-5")
        assert ok.data["cost_usd"] == pytest.approx(0.5)
        unknown = await a.call("report_usage", tokens_in=500_000, tokens_out=0, latency_ms=1.0, model="other")
        assert unknown.data["cost_usd"] == pytest.approx(2.0)
        slow = await a.call("report_usage", tokens_in=1, tokens_out=1, latency_ms=500.0, model="other")
        assert slow.code == "budget_breached"
        assert slow.data["details"]["breaches"][0]["kind"] == "latency"
        assert (await a.call("forecast", model="m", symbols=["X"], horizon=1)).code == "session_frozen"
        assert (await a.call("clock")).data["status"] == "frozen"


async def test_unknown_model_refused_by_policy(settings: Settings) -> None:
    s2 = with_updates(settings, "pricing", unknown_model_policy=UnknownModelPolicy.REFUSE)
    h = Harness.create(s2, admin_key=ADMIN_KEY)
    s = await paper(h)
    async with h.agent(s["api_key"]) as a:
        out = await a.call("report_usage", tokens_in=1, tokens_out=1, latency_ms=1.0, model="mystery")
    assert out.code == "invalid_request"
    await h.gateway.downstream.aclose()


async def test_verdict_with_leakage_audit(harness: Harness) -> None:
    s = await research(harness)
    async with harness.agent(s["api_key"]) as a:
        assert (await a.call("verdict", strategy_id="mom")).code == "not_found"
        await a.call(
            "run_backtest",
            strategy="mom",
            params={},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        plain = await a.call("verdict", strategy_id="mom")
        assert plain.data["leakage_status"] == "not_required"
        assert not plain.data["llm_driven"]
        await a.call("report_usage", tokens_in=10, tokens_out=10, latency_ms=1.0, model="leaky-llm")
        flagged = await a.call("verdict", strategy_id="mom")
    assert flagged.data["leakage_status"] == "flagged"
    assert not flagged.data["promotable"]
    report = flagged.data["leakage_reports"][0]
    assert report["model_id"] == "leaky-llm"
    assert report["cells"][0]["entity"] == "SBER"
    assert harness.platform.ledger.leakage[0].source == "s7"
    assert harness.platform.ledger.leakage[0].strategy_id == "ag:mom"


async def test_downstream_outages(settings: Settings) -> None:
    h = Harness.create(settings, admin_key=ADMIN_KEY, down={"ledger", "backtest", "leakage"})
    s = await research(h)
    async with h.agent(s["api_key"]) as a:
        out = await a.call(
            "run_backtest",
            strategy="mom",
            params={},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        assert out.code == "downstream_unavailable"
        assert out.data["details"]["service"] == "backtest"
        assert (await a.call("verdict", strategy_id="mom")).code == "downstream_unavailable"
        budget = await a.call("budget")
        assert budget.data["usage"]["trials_inboxed"] == 1
    inbox = list(h.settings.ledger_inbox_dir.glob("i9-*.jsonl"))
    assert len(inbox) == 1
    assert "refused:downstream_unavailable" in inbox[0].read_text()
    await h.gateway.downstream.aclose()


async def test_not_audited_when_probe_down(settings: Settings) -> None:
    h = Harness.create(settings, admin_key=ADMIN_KEY, down={"leakage"})
    s = await research(h)
    async with h.agent(s["api_key"]) as a:
        await a.call(
            "run_backtest",
            strategy="mom",
            params={},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        await a.call("report_usage", tokens_in=1, tokens_out=1, latency_ms=1.0, model="m")
        out = await a.call("verdict", strategy_id="mom")
    assert out.data["leakage_status"] == "not_audited"
    assert not out.data["promotable"]
    await h.gateway.downstream.aclose()


async def test_live_mode_gate(settings: Settings) -> None:
    disabled = Harness.create(settings, admin_key=ADMIN_KEY)
    with pytest.raises(httpx.HTTPStatusError) as info:
        await disabled.create_session(agent_id="ag", mode="live")
    assert info.value.response.status_code == 403
    await disabled.gateway.downstream.aclose()

    live = with_updates(settings, "modes", live_enabled=True)
    strict = with_updates(live, "fakes", verdict_min_sharpe=100.0)
    lenient = with_updates(live, "fakes", verdict_min_sharpe=-100.0)
    for cfg, expect_routed in ((strict, False), (lenient, True)):
        h = Harness.create(cfg, admin_key=ADMIN_KEY)
        r = await research(h)
        async with h.agent(r["api_key"]) as a:
            await a.call(
                "run_backtest",
                strategy="mom",
                params={},
                universe=["SBER-RUB-TQBR"],
                start="2025-05-01",
                end="2025-06-01",
                fidelity="event",
            )
        s = await h.create_session(agent_id="ag", mode="live")
        async with h.agent(s["api_key"]) as a:
            out = await a.call("place_order", **order(h))
        if expect_routed:
            assert out.data["routed_to"] == "i7"
            assert len(h.platform.execution.received) == 1
        else:
            assert out.code == "live_gate_failed"
        assert len(h.platform.exchange.received) == 0
        await h.gateway.downstream.aclose()


async def test_live_disabled_after_session_creation(settings: Settings) -> None:
    h = Harness.create(with_updates(settings, "modes", live_enabled=True), admin_key=ADMIN_KEY)
    s = await h.create_session(agent_id="ag", mode="live")
    h.gateway.settings = with_updates(h.gateway.settings, "modes", live_enabled=False)
    async with h.agent(s["api_key"]) as a:
        assert (await a.call("place_order", **order(h))).code == "live_disabled"
    await h.gateway.downstream.aclose()


async def test_unauthenticated_and_closed(harness: Harness) -> None:
    async with harness.agent("wrong-key") as a:
        assert (await a.call("clock")).code == "unauthenticated"
    s = await research(harness)
    async with harness.admin_client() as admin:
        await admin.post(f"{harness.settings.server.admin_prefix}/sessions/{s['session_id']}/close")
    async with harness.agent(s["api_key"]) as a:
        assert (await a.call("clock")).code == "session_closed"


async def test_agent_connection_is_reusable(harness: Harness) -> None:
    s = await research(harness)
    async with harness.agent(s["api_key"]) as a:
        assert isinstance(a, AgentConnection)
        replies = [await a.call("clock") for _ in range(3)]
    assert all(not r.is_error for r in replies)


async def test_order_rate_window_runs_on_session_clock(harness: Harness) -> None:
    """A replayed paper episode moves the session clock, not the wall clock; the rate window must follow it."""
    known = harness.settings.demo.known_at
    s = await harness.create_session(
        agent_id="ag", mode="paper", known_at=known, budgets={"max_orders_per_window": 2}, broker="shadow"
    )
    async with harness.agent(s["api_key"]) as a:
        assert not (await a.call("place_order", **order(harness))).is_error
        assert not (await a.call("place_order", **order(harness, side="sell"))).is_error
        third = await a.call("place_order", **order(harness))
        assert third.code == "budget_breached"
        assert third.data["details"]["breaches"][0]["kind"] == "order_rate"
        moved = await harness.set_clock(s["session_id"], to_iso(parse_iso(known) + timedelta(seconds=61)))
        assert moved.status_code == 200
        assert not (await a.call("place_order", **order(harness))).is_error
        remaining = (await a.call("budget")).data["remaining"]["orders_in_window"]
    assert remaining == 1.0


async def test_paper_fills_at_session_clock_price(harness: Harness) -> None:
    known = parse_iso(harness.settings.demo.known_at)
    world = harness.platform.world
    symbol = harness.settings.demo.symbol
    s = await harness.create_session(agent_id="ag", mode="paper", known_at=to_iso(known), broker="shadow")
    async with harness.agent(s["api_key"]) as a:
        first = await a.call("place_order", **order(harness))
        await harness.set_clock(s["session_id"], to_iso(known + timedelta(hours=1)))
        second = await a.call("place_order", **order(harness, side="sell"))
    for reply, at in ((first, known), (second, known + timedelta(hours=1))):
        fill = reply.data["order"]["price"]
        assert fill == pytest.approx(reply.data["reference_price"])
        assert fill == pytest.approx(world.last_known_price(symbol, at))
    assert first.data["order"]["price"] != pytest.approx(world.last_known_price(symbol, harness.clock.now()))
    assert first.data["order"]["price"] != pytest.approx(second.data["order"]["price"])


async def test_verdict_resolves_the_strategy_id_given_to_i5(harness: Harness) -> None:
    """I5 logs the backtest under the id the gateway sends; verdict() must ask S6 for that same id."""
    s = await research(harness)
    async with harness.agent(s["api_key"]) as a:
        bt = await a.call(
            "run_backtest",
            strategy="mom",
            params={"seed": 7},
            universe=["SBER-RUB-TQBR"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        verdict = await a.call("verdict", strategy_id="mom")
    sent = harness.platform.backtests[-1]
    assert sent.strategy_id == bt.data["ledger_strategy_id"] == "ag:mom"
    assert sent.actor is not None
    assert (sent.actor.kind, sent.actor.id) == ("agent", "ag")
    assert sent.seed == 7
    assert "seed" not in sent.params
    assert not verdict.is_error
    assert verdict.data["ledger_strategy_id"] == sent.strategy_id
    assert verdict.data["verdict"]["strategy_id"] == sent.strategy_id
    assert verdict.data["verdict"]["n_trials"] == 1
    logged = [t for t in harness.platform.ledger.trials if t.strategy_id == sent.strategy_id]
    assert [t.trial_id for t in logged] == [bt.data["backtest_trial_id"]]


async def test_default_paper_broker_is_the_mirror_of_the_real_book(harness: Harness) -> None:
    """Contract 6.8: paper sessions trade a copy of the market read from I2, not a broker."""
    known = harness.settings.demo.known_at
    s = await harness.create_session(agent_id="mir", mode="paper", known_at=known)
    assert s["broker"] == "mirror"
    async with harness.agent(s["api_key"]) as a:
        sl = await a.call("market_slice", symbols=[harness.settings.demo.symbol])
        inst = sl.data["slice"]["instruments"][0]
        assert sl.data["known_at"] == known
        assert inst["best_ask"] > inst["best_bid"]
        rules = await a.call("instrument", venue=harness.settings.demo.venue, symbol=harness.settings.demo.symbol)
        assert rules.data["broker"] == "mirror"
        assert rules.data["rules"]["lot_size"] == 1.0
        out = await a.call("place_order", **order(harness, qty=10.0))
        assert out.data["routed_to"] == "mirror"
        assert out.data["order"]["status"] == "filled"
        assert out.data["order"]["avg_price"] == pytest.approx(inst["best_ask"])  # took the touch
        fraction = await a.call("place_order", **order(harness, qty=0.5))
        failed = {c["name"] for c in fraction.data["details"]["checks"] if not c["passed"]}
        assert fraction.code == "pre_trade_rejected"
        assert failed == {"qty_whole_lots"}
        off_tick = await a.call("place_order", **order(harness, type="limit", price=round(inst["best_bid"], 2) + 0.005))
        assert {c["name"] for c in off_tick.data["details"]["checks"] if not c["passed"]} == {"price_on_tick"}
        rest = await a.call("place_order", **order(harness, type="limit", price=round(inst["best_bid"] - 0.5, 2)))
        assert rest.data["order"]["status"] == "accepted"
        assert (await a.call("cancel_order", order_id=rest.data["order"]["order_id"])).data["order"]["status"] == (
            "cancelled"
        )
        assert (await a.call("cancel_order", order_id="mir-none")).code == "not_found"
        pos = await a.call("positions")
        assert pos.data["positions"][0]["qty"] == 10.0
        acct = await a.call("account")
        assert acct.data["account"]["currency"] == "RUB"
    assert len(harness.platform.exchange.received) == 0  # nothing went to I6


async def test_session_broker_choice_is_validated(harness: Harness) -> None:
    async with harness.admin_client() as client:
        prefix = harness.settings.server.admin_prefix
        bad = await client.post(f"{prefix}/sessions", json={"agent_id": "x", "mode": "paper", "broker": "execution"})
        assert bad.status_code == 422
        assert bad.json()["code"] == "invalid_broker"


def tbank_enabled(settings: Settings) -> Settings:
    cfg = settings.brokers
    brokers = cfg.model_copy(
        update={
            "allowed_paper": [*cfg.allowed_paper, "tbank-sandbox"],
            "tbank_sandbox": cfg.tbank_sandbox.model_copy(update={"enabled": True}),
        }
    )
    return settings.model_copy(update={"brokers": brokers})


async def test_tbank_sandbox_is_refused_while_disabled(harness: Harness) -> None:
    """Trading is local only: no session can pick the T-Bank sandbox unless config enables it."""
    async with harness.admin_client() as client:
        prefix = harness.settings.server.admin_prefix
        body = {"agent_id": "x", "mode": "paper", "broker": "tbank-sandbox"}
        refused = await client.post(f"{prefix}/sessions", json=body)
    assert refused.status_code == 403
    assert refused.json()["code"] == "broker_disabled"
    assert "brokers.tbank_sandbox.enabled" in refused.json()["message"]
    assert (await harness.create_session(agent_id="x", mode="paper"))["broker"] == "mirror"


async def test_stored_tbank_session_is_refused_after_disabling(settings: Settings) -> None:
    """A session created while T-Bank was enabled cannot reach it once the flag is off again."""
    on = Harness.create(tbank_enabled(settings), admin_key=ADMIN_KEY)
    s = await on.create_session(agent_id="tb", mode="paper", broker="tbank-sandbox")
    await on.gateway.downstream.aclose()
    off = Harness.create(settings, admin_key=ADMIN_KEY)
    try:
        async with off.agent(s["api_key"]) as a:
            out = await a.call("place_order", **order(off))
        assert out.code == "mode_forbidden"
        assert "disabled" in out.data["message"]
    finally:
        await off.gateway.downstream.aclose()


async def test_tbank_sandbox_without_token_is_a_structured_refusal(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TBANK_TOKEN", raising=False)
    harness = Harness.create(tbank_enabled(settings), admin_key=ADMIN_KEY)
    try:
        s = await harness.create_session(agent_id="tb", mode="paper", broker="tbank-sandbox")
        async with harness.agent(s["api_key"]) as a:
            out = await a.call("instrument", venue="moex-tbank", symbol="SBER-RUB-TQBR")
    finally:
        await harness.gateway.downstream.aclose()
    assert out.code == "downstream_unavailable"
    assert "TBANK_TOKEN" in out.data["message"]


async def test_mirror_fee_reaches_fills_account_and_trial_log(harness: Harness) -> None:
    """0.25% per fill on both sides; the fee is in the order's fills, the account PnL and the S6 order intent."""
    s = await harness.create_session(agent_id="fee", mode="paper", known_at=harness.settings.demo.known_at)
    rate = harness.settings.brokers.mirror.commission.rate
    async with harness.agent(s["api_key"]) as a:
        buy = (await a.call("place_order", **order(harness, qty=10.0))).data["order"]
        sell = (await a.call("place_order", **order(harness, side="sell", qty=10.0))).data["order"]
        acct = (await a.call("account")).data["account"]
    for o in (buy, sell):
        assert o["fee"] == pytest.approx(round(o["avg_price"] * 10.0 * rate, 2), abs=0.01)
        assert o["fee"] > 0
        assert sum(f["fee"] for f in o["fills"]) == pytest.approx(o["fee"])
    fees = buy["fee"] + sell["fee"]
    assert acct["fees"] == pytest.approx(fees)
    spread = (buy["avg_price"] - sell["avg_price"]) * 10.0
    assert acct["pnl"] == pytest.approx(-spread - fees)
    intents = [t for t in harness.platform.ledger.trials if "order-intent" in t.tags and t.strategy_id == "fee:mom"]
    assert [t.metrics["fee"] for t in intents] == [buy["fee"], sell["fee"]]
    assert all(t.metrics["filled_qty"] == 10.0 for t in intents)
