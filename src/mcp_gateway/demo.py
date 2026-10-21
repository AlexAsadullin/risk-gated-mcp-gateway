"""``make reproduce``: a scripted agent drives the real gateway against in-process fakes.

The script shows a time-travel refusal, a logged backtest trial, a paper order that reaches the fake I6, a
fat-finger rejection and a budget breach that freezes the session, then returns the audit log.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp_gateway.audit import AuditEntry, verify_chain
from mcp_gateway.harness import AgentConnection, Harness, ToolReply
from mcp_gateway.settings import Settings
from mcp_gateway.timeutil import parse_iso, to_iso

DEMO_ADMIN_KEY = "demo-admin-key-not-a-secret"


@dataclass
class DemoStep:
    name: str
    tool: str
    outcome: str
    detail: str


@dataclass
class DemoResult:
    steps: list[DemoStep] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    chain_intact: bool = False
    ledger_trials: int = 0
    exchange_orders: int = 0
    summary_path: str = ""

    def record(self, name: str, tool: str, reply: ToolReply, detail: str) -> ToolReply:
        outcome = f"refused:{reply.code}" if reply.is_error else "ok"
        self.steps.append(DemoStep(name=name, tool=tool, outcome=outcome, detail=detail))
        return reply


def demo_settings(settings: Settings) -> Settings:
    """Point sessions and audit at the demo folder so every run starts clean and is self-contained."""
    demo = settings.storage.demo_dir
    storage = settings.storage.model_copy(
        update={"sessions_db": f"{demo}/sessions.sqlite3", "audit_dir": f"{demo}/audit"}
    )
    return settings.model_copy(update={"storage": storage})


async def _research(agent: AgentConnection, s: Settings, result: DemoResult) -> None:
    d = s.demo
    known_at = parse_iso(d.known_at)
    hour_before = to_iso(known_at - timedelta(hours=1))
    day_after = to_iso(known_at + timedelta(days=1))
    reply = await agent.call("clock")
    result.record("read session clock", "clock", reply, f"known_at={reply.data.get('known_at')}")
    reply = await agent.call(
        "market_data", venue=d.venue, symbol=d.symbol, channel="trades", start=hour_before, end=d.known_at
    )
    result.record("trades for the last hour", "market_data", reply, f"rows={reply.data.get('n_rows')}")
    reply = await agent.call(
        "market_data", venue=d.venue, symbol=d.symbol, channel="trades", start=hour_before, end=day_after
    )
    result.record("ask for tomorrow's trades", "market_data", reply, reply.data.get("message", ""))
    reply = await agent.call("forecast", model="chronos-bolt-small", symbols=[d.symbol], horizon=24)
    mean = reply.data.get("forecasts", [{}])[0].get("mean")
    result.record("24-bar forecast", "forecast", reply, f"mean={mean}")
    reply = await agent.call(
        "run_backtest",
        strategy="tsmom",
        params={"lookback": 24, "seed": 1},
        universe=[d.symbol],
        start="2025-01-01T00:00:00Z",
        end=d.known_at,
        fidelity="vectorised",
    )
    detail = f"trial={reply.data.get('gateway_trial_id')} logged_to={reply.data.get('logged_to')}"
    result.record("backtest tsmom", "run_backtest", reply, detail)
    reply = await agent.call(
        "run_backtest",
        strategy="tsmom",
        params={"lookback": 48, "seed": 1},
        universe=[d.symbol],
        start="2025-01-01T00:00:00Z",
        end=day_after,
        fidelity="vectorised",
    )
    result.record("backtest that peeks past known_at", "run_backtest", reply, "refused, but still logged")
    await agent.call("report_usage", tokens_in=1200, tokens_out=300, latency_ms=900.0, model=d.model)
    reply = await agent.call("verdict", strategy_id="tsmom")
    detail = f"passed={reply.data.get('verdict', {}).get('passed')} leakage={reply.data.get('leakage_status')}"
    result.record("ledger verdict + S7 audit", "verdict", reply, detail)
    reply = await agent.call(
        "place_order",
        venue=d.venue,
        symbol=d.symbol,
        side="buy",
        type="market",
        qty=0.01,
        strategy_id="tsmom",
        rationale="research sessions cannot trade",
    )
    result.record("order from a research session", "place_order", reply, reply.data.get("message", ""))


async def _paper(agent: AgentConnection, s: Settings, result: DemoResult) -> None:
    d = s.demo
    reply = await agent.call(
        "place_order",
        venue=d.venue,
        symbol=d.symbol,
        side="buy",
        type="market",
        qty=0.05,
        strategy_id="tsmom",
        rationale="momentum is positive over 24 bars",
    )
    order = reply.data.get("order", {})
    result.record(
        "paper market order", "place_order", reply, f"{order.get('status')} via {reply.data.get('routed_to')}"
    )
    ref = float(reply.data.get("reference_price") or 0.0)
    reply = await agent.call(
        "place_order",
        venue=d.venue,
        symbol=d.symbol,
        side="buy",
        type="limit",
        qty=0.05,
        price=round(ref * 1.10, 2),
        strategy_id="tsmom",
        rationale="fat-finger: limit 10% through the market",
    )
    result.record("fat-finger limit order", "place_order", reply, reply.data.get("message", ""))
    reply = await agent.call("positions")
    result.record("positions", "positions", reply, f"n={len(reply.data.get('positions', []))}")
    reply = await agent.call("report_usage", tokens_in=2_500_000, tokens_out=50_000, latency_ms=4000.0, model=d.model)
    result.record("token budget breach", "report_usage", reply, reply.data.get("message", ""))
    reply = await agent.call(
        "market_data", venue=d.venue, symbol=d.symbol, channel="trades", start=d.known_at, end=d.wall_clock
    )
    result.record("tool call after freeze", "market_data", reply, reply.data.get("message", ""))
    reply = await agent.call("budget")
    result.record("budget after freeze", "budget", reply, f"status={reply.data.get('status')}")


async def run_demo(settings: Settings) -> DemoResult:
    settings = demo_settings(settings)
    shutil.rmtree(settings.demo_dir, ignore_errors=True)
    harness = Harness.create(settings, admin_key=DEMO_ADMIN_KEY)
    result = DemoResult()
    d = settings.demo
    research = await harness.create_session(agent_id=d.agent_id, mode="research", known_at=d.known_at)
    async with harness.agent(research["api_key"]) as agent:
        await _research(agent, settings, result)
    paper = await harness.create_session(agent_id=d.agent_id, mode="paper", known_at=None)
    async with harness.agent(paper["api_key"]) as agent:
        await _paper(agent, settings, result)
    await harness.gateway.downstream.aclose()
    result.audit = harness.gateway.audit.read()
    result.chain_intact = all(verify_chain(p) is None for p in harness.gateway.audit.files())
    result.ledger_trials = len(harness.platform.ledger.trials)
    result.exchange_orders = len(harness.platform.exchange.received)
    summary = {
        "steps": [step.__dict__ for step in result.steps],
        "ledger_trials": result.ledger_trials,
        "exchange_orders": result.exchange_orders,
        "audit_entries": len(result.audit),
        "audit_chain_intact": result.chain_intact,
    }
    path = settings.demo_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2))
    result.summary_path = str(path)
    return result


def audit_line(entry: AuditEntry) -> str:
    args: dict[str, Any] = {k: v for k, v in entry.args.items() if k != "rationale"}
    code = f" {entry.code}" if entry.code else ""
    return f"{entry.seq:3d} {entry.ts} {entry.actor:<11} {entry.action:<14} {entry.decision}{code} {json.dumps(args)}"
