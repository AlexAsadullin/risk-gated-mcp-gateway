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
        "qty": 0.01,
        "strategy_id": "mom",
        "rationale": "test",
    }
    return base | kw


async def research(h: Harness) -> dict[str, Any]:
    return await h.create_session(agent_id="ag", mode="research", known_at=h.settings.demo.known_at)


async def paper(h: Harness, **budgets: Any) -> dict[str, Any]:
    return await h.create_session(agent_id="ag", mode="paper", known_at=None, budgets=budgets)


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
            symbols=["BTC-USDT-PERP"],
            start=window(harness)["start"],
            end=window(harness)["end"],
        )
        assert not feats.is_error
        assert "ret_1h" in feats.data["rows"][0]
        fc = await a.call("forecast", model="m", symbols=["BTC-USDT-PERP"], horizon=4)
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
            universe=["BTC-USDT-PERP"],
            start="2025-05-01",
            end="2025-06-01",
            fidelity="event",
        )
        assert not bt.is_error
        assert bt.data["logged_to"] == "ledger"
        assert bt.data["ledger_strategy_id"] == "ag:mom"
        peek = await a.call(
            "run_backtest",
            strategy="mom",
            params={},
            universe=["BTC-USDT-PERP"],
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
    assert trials[0].params["linked_trial_id"]
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
        fat = await a.call("place_order", **order(harness, type="limit", price=ref * 1.2))
        assert fat.code == "pre_trade_rejected"
        failed = {c["name"] for c in fat.data["details"]["checks"] if not c["passed"]}
        assert failed == {"price_band_bps"}
        assert (await a.call("place_order", **order(harness, type="limit"))).code == "pre_trade_rejected"
        assert (await a.call("place_order", **order(harness, venue="kraken"))).code == "pre_trade_rejected"
        big = await a.call("place_order", **order(harness, qty=0.2))
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
        assert pos.data["positions"][0]["qty"] == pytest.approx(0.02)
        acct = await a.call("account")
        assert acct.data["drawdown"] == pytest.approx(0.0, abs=1e-3)
        budget = await a.call("budget")
        assert budget.data["usage"]["orders_submitted"] == 3
        assert budget.data["status"] == "active"
    intents = [t for t in harness.platform.ledger.trials if "order-intent" in t.tags]
    assert len(intents) == 8
    assert len(harness.platform.exchange.received) == 3


async def test_cancel_other_account_is_not_found(harness: Harness) -> None:
    first = await harness.create_session(agent_id="one", mode="paper")
    second = await harness.create_session(agent_id="two", mode="paper")
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
            assert not (await a.call("place_order", **order(harness, qty=0.15))).is_error
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
        assert unknown.data["cost_usd"] == pytest.approx(1.5)
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
            universe=["BTC-USDT-PERP"],
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
    assert report["cells"][0]["entity"] == "BTC"
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
            universe=["BTC-USDT-PERP"],
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
            universe=["BTC-USDT-PERP"],
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
    lenient = with_updates(live, "fakes", verdict_min_sharpe=-100.0)
    for cfg, expect_routed in ((live, False), (lenient, True)):
        h = Harness.create(cfg, admin_key=ADMIN_KEY)
        r = await research(h)
        async with h.agent(r["api_key"]) as a:
            await a.call(
                "run_backtest",
                strategy="mom",
                params={},
                universe=["BTC-USDT-PERP"],
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
    s = await harness.create_session(agent_id="ag", mode="paper", known_at=known, budgets={"max_orders_per_window": 2})
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
    s = await harness.create_session(agent_id="ag", mode="paper", known_at=to_iso(known))
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
