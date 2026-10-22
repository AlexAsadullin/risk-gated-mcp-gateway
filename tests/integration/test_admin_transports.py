"""Admin HTTP API, streamable HTTP and stdio transports, demo, report and CLI."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from mcp_gateway.app import build_gateway, build_http_app
from mcp_gateway.bench.latency import serve_loopback
from mcp_gateway.bench.report import run_report
from mcp_gateway.cli import app as cli_app
from mcp_gateway.demo import run_demo
from mcp_gateway.harness import Harness, decode
from mcp_gateway.settings import Settings
from tests.conftest import ADMIN_KEY, with_updates


async def test_admin_api(harness: Harness) -> None:
    prefix = harness.settings.server.admin_prefix
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.http.app), base_url="http://t") as anon:
        assert (await anon.post(f"{prefix}/sessions", json={})).status_code == 401
        bad = await anon.get(f"{prefix}/sessions", headers={"authorization": "Bearer nope"})
        assert bad.status_code == 401
        assert (await anon.get("/healthz")).json()["status"] == "ok"
    async with harness.admin_client() as admin:
        no_clock = await admin.post(f"{prefix}/sessions", json={"agent_id": "a", "mode": "research"})
        assert no_clock.status_code == 422
        assert no_clock.json()["code"] == "invalid_clock"
        future = await admin.post(
            f"{prefix}/sessions", json={"agent_id": "a", "mode": "research", "known_at": "2030-01-01"}
        )
        assert future.status_code == 422
        garbage = await admin.post(f"{prefix}/sessions", json={"agent_id": "a", "mode": "research", "known_at": "x"})
        assert garbage.status_code == 422
        created = await admin.post(
            f"{prefix}/sessions",
            json={
                "agent_id": "a",
                "mode": "research",
                "known_at": "2025-06-01T00:00:00Z",
                "budgets": {"max_tokens": 10},
                "account_id": "acct-1",
            },
        )
        assert created.status_code == 201
        body = created.json()
        sid = body["session_id"]
        assert body["api_key"]
        assert body["limits"]["max_tokens"] == 10
        assert body["account_id"] == "acct-1"
        assert body["schema_version"] == "1"
        assert body["mcp_path"] == "/mcp"
        stored = harness.gateway.store.get(sid)
        assert stored is not None
        assert body["api_key"] not in stored.api_key_hash
        forward = await admin.post(f"{prefix}/sessions/{sid}/clock", json={"known_at": "2025-06-02T00:00:00Z"})
        assert forward.json()["known_at"] == "2025-06-02T00:00:00Z"
        rewind = await admin.post(f"{prefix}/sessions/{sid}/clock", json={"known_at": "2025-06-01T00:00:00Z"})
        assert rewind.status_code == 409
        assert rewind.json()["code"] == "clock_rewind"
        assert (await admin.post(f"{prefix}/sessions/{sid}/clock", json={"known_at": "?"})).status_code == 422
        assert (
            await admin.post(f"{prefix}/sessions/missing/clock", json={"known_at": "2025-01-01"})
        ).status_code == 404
        assert (await admin.get(f"{prefix}/sessions/{sid}")).json()["status"] == "active"
        assert len((await admin.get(f"{prefix}/sessions", params={"agent_id": "a"})).json()) == 1
        async with harness.agent(body["api_key"]) as agent:
            frozen = await agent.call("report_usage", tokens_in=100, tokens_out=0, latency_ms=1.0, model="m")
            assert frozen.data["session_status"] == "frozen"
        unfrozen = await admin.post(f"{prefix}/sessions/{sid}/unfreeze")
        assert unfrozen.json()["status"] == "active"
        assert unfrozen.json()["freeze"] is None
        audit = (await admin.get(f"{prefix}/sessions/{sid}/audit")).json()
        assert [e["action"] for e in audit] == ["create_session", "set_clock", "report_usage", "set_status:active"]
        assert (await admin.post(f"{prefix}/sessions/{sid}/close")).json()["status"] == "closed"
        assert (await admin.post(f"{prefix}/sessions/{sid}/unfreeze")).status_code == 409


async def test_admin_disabled_without_key(settings: Settings) -> None:
    http = build_http_app(build_gateway(settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http.app), base_url="http://t") as client:
        out = await client.get(f"{settings.server.admin_prefix}/sessions", headers={"authorization": "Bearer x"})
    assert out.status_code == 503
    await http.gateway.downstream.aclose()


async def test_streamable_http_transport(harness: Harness) -> None:
    session = await harness.create_session(agent_id="h", mode="research", known_at=harness.settings.demo.known_at)
    path = harness.settings.server.mcp_path
    async with serve_loopback(harness.http.app) as base:
        async with httpx2.AsyncClient() as raw:
            assert (await raw.post(base + path, json={})).status_code == 401
        for headers in (
            {"authorization": f"Bearer {session['api_key']}"},
            {"authorization": f"Bearer {ADMIN_KEY}", "x-gateway-session": session["session_id"]},
        ):
            async with (
                httpx2.AsyncClient(headers=headers) as http_client,
                Client(streamable_http_client(base + path, http_client=http_client)) as client,
            ):
                reply = decode(await client.call_tool("clock", {}))
                assert reply.data["session_id"] == session["session_id"]
        async with (
            httpx2.AsyncClient(headers={"authorization": "Bearer wrong"}) as http_client,
            Client(streamable_http_client(base + path, http_client=http_client)) as client,
        ):
            assert decode(await client.call_tool("clock", {})).code == "unauthenticated"


async def test_stdio_transport(harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = await harness.create_session(agent_id="s", mode="research", known_at=harness.settings.demo.known_at)
    env = {
        "TRADEQ_DATA_DIR": str(harness.settings.data_dir),
        "GATEWAY_AGENT_API_KEY": session["api_key"],
        "PATH": "/usr/bin:/bin",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_gateway", "stdio", "--offline"], env=env)
    async with Client(params) as client:
        clock = decode(await client.call_tool("clock", {}))
        data = decode(
            await client.call_tool(
                "market_data",
                {
                    "venue": "binance",
                    "symbol": "BTC-USDT-PERP",
                    "channel": "trades",
                    "start": "2025-05-31T23:00:00Z",
                    "end": "2025-06-01T00:00:00Z",
                },
            )
        )
    assert clock.data["session_id"] == session["session_id"]
    assert data.data["n_rows"] == 60
    assert any(e.action == "market_data" for e in harness.gateway.audit.read(session["session_id"]))


async def test_demo(settings: Settings) -> None:
    result = await run_demo(settings)
    outcomes = {s.name: s.outcome for s in result.steps}
    assert outcomes["ask for tomorrow's trades"] == "refused:time_travel"
    assert outcomes["backtest tsmom"] == "ok"
    assert outcomes["paper market order"] == "ok"
    assert outcomes["fat-finger limit order"] == "refused:pre_trade_rejected"
    assert outcomes["token budget breach"] == "refused:budget_breached"
    assert outcomes["tool call after freeze"] == "refused:session_frozen"
    assert result.chain_intact
    assert result.exchange_orders == 1
    assert result.ledger_trials == 5
    assert json.loads(Path(result.summary_path).read_text())["audit_chain_intact"]


async def test_report_small(settings: Settings) -> None:
    small = with_updates(
        settings, "demo", report_episodes=3, report_latency_calls=5, report_latency_repeats=2, report_bootstrap=50
    )
    metrics = await run_report(small)
    by_arm = {(r["arm"], r["attack"]): r for r in metrics["attacks"]}
    for (arm, _), row in by_arm.items():
        if row["violations"]:
            assert row["block_rate"] == (1.0 if arm == "guarded" else 0.0)
        assert row["false_refusals"] == 0
    transports = {r["transport"] for r in metrics["latency"]}
    assert transports == {"baseline_direct", "direct", "mcp_in_process", "mcp_stdio", "mcp_streamable_http"}
    assert (small.reports_dir / "tables.md").read_text().startswith("Table 1")


def test_cli_session_create_and_reproduce(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    out = runner.invoke(
        cli_app, ["session-create", "--agent-id", "c", "--mode", "research", "--known-at", "2025-06-01T00:00:00Z"]
    )
    assert out.exit_code == 0
    assert json.loads(out.output)["agent_id"] == "c"
    out = runner.invoke(cli_app, ["reproduce"])
    assert out.exit_code == 0
    assert "hash chain intact: True" in out.output
    out = runner.invoke(cli_app, ["stdio"])
    assert out.exit_code != 0


def test_serve_offline_subprocess(tmp_path: Path) -> None:
    """`gateway serve --offline` serves admin + MCP over HTTP against the fakes, with no launcher of its own."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": "/usr/bin:/bin",
        "TRADEQ_DATA_DIR": str(tmp_path / "data"),
        "GATEWAY_ADMIN_API_KEY": ADMIN_KEY,
        "GATEWAY_SERVER__PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_gateway", "serve", "--offline"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            try:
                if httpx.get(base + "/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        admin = {"authorization": f"Bearer {ADMIN_KEY}"}
        created = httpx.post(
            base + "/admin/v1/sessions",
            headers=admin,
            json={"agent_id": "off", "mode": "paper", "known_at": "2025-06-01T00:00:00Z"},
        )
        assert created.status_code == 201
        key = created.json()["api_key"]

        async def call() -> dict[str, object]:
            async with (
                httpx2.AsyncClient(headers={"authorization": f"Bearer {key}"}) as http_client,
                Client(streamable_http_client(base + "/mcp", http_client=http_client)) as client,
            ):
                return decode(
                    await client.call_tool(
                        "place_order",
                        {
                            "venue": "binance",
                            "symbol": "BTC-USDT-PERP",
                            "side": "buy",
                            "type": "market",
                            "qty": 0.01,
                            "strategy_id": "s",
                            "rationale": "offline serve",
                        },
                    )
                ).data

        data = asyncio.run(call())
        assert data["routed_to"] == "i6"
        assert data["order"]["price"] == pytest.approx(data["reference_price"])
    finally:
        proc.terminate()
        proc.wait(timeout=10)
