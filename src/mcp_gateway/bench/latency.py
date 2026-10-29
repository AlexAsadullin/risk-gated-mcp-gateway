"""Per-call latency of the gateway over each transport: direct call, in-process MCP, stdio and streamable HTTP."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Literal

import httpx2
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from mcp_gateway.bench.baseline import PassThroughWrapper
from mcp_gateway.gateway import Credentials
from mcp_gateway.harness import Harness, decode
from mcp_gateway.mcp_server import build_mcp_server, fixed_credentials
from mcp_gateway.refusals import RefusalBody
from mcp_gateway.timeutil import parse_iso, to_iso

MS_PER_S = 1000.0
LOOPBACK = "127.0.0.1"


@asynccontextmanager
async def serve_loopback(app: Any) -> AsyncIterator[str]:
    """Run ``app`` with uvicorn on an ephemeral loopback port; yield its base URL."""
    config = uvicorn.Config(app, host=LOOPBACK, port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes no startup event
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://{LOOPBACK}:{port}"
    finally:
        server.should_exit = True
        await task


async def time_calls(call: Callable[[], Awaitable[Any]], n: int, warmup: int) -> list[float]:
    for _ in range(warmup):
        await call()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        await call()
        out.append((time.perf_counter() - t0) * MS_PER_S)
    return out


def _window(h: Harness) -> dict[str, str]:
    """A one-hour trades window that ends exactly at the research session clock."""
    d = h.settings.demo
    start = to_iso(parse_iso(d.known_at) - timedelta(hours=1))
    return {"venue": d.venue, "symbol": d.symbol, "channel": "trades", "start": start, "end": d.known_at}


async def measure_transports(h: Harness, n: int, warmup: int) -> dict[str, dict[str, list[float]]]:
    """Latency samples (ms) per transport for ``clock`` (no downstream call) and ``market_data`` (one I2 call)."""
    d = h.settings.demo
    session = await h.create_session(agent_id="latency", mode="research", known_at=d.known_at)
    key = session["api_key"]
    args = _window(h)
    creds = Credentials(api_key=key)
    out: dict[str, dict[str, list[float]]] = {}

    wrapper = PassThroughWrapper(h.settings, h.gateway.downstream, h.clock, "latency")
    out["baseline_direct"] = {
        "market_data": await time_calls(lambda: wrapper.market_data(**args), n, warmup),
    }
    out["direct"] = {
        "clock": await time_calls(lambda: h.gateway.clock_tool(creds), n, warmup),
        "market_data": await time_calls(lambda: h.gateway.market_data(creds, **args), n, warmup),
    }

    async def via(client: Client) -> dict[str, list[float]]:
        async def tool(name: str, arguments: dict[str, Any]) -> None:
            reply = decode(await client.call_tool(name, arguments))
            if reply.is_error:
                raise RuntimeError(f"{name} refused during latency run: {reply.data}")

        return {
            "clock": await time_calls(lambda: tool("clock", {}), n, warmup),
            "market_data": await time_calls(lambda: tool("market_data", args), n, warmup),
        }

    async with Client(build_mcp_server(h.gateway, fixed_credentials(key))) as client:
        out["mcp_in_process"] = await via(client)

    env = {
        **os.environ,
        "TRADEQ_DATA_DIR": str(h.settings.data_dir),
        "GATEWAY_AGENT_API_KEY": key,
        "GATEWAY_STORAGE__SESSIONS_DB": h.settings.storage.sessions_db,
        "GATEWAY_STORAGE__AUDIT_DIR": h.settings.storage.audit_dir,
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_gateway", "stdio", "--offline"], env=env)
    async with Client(params) as client:
        out["mcp_stdio"] = await via(client)

    async with serve_loopback(h.http.app) as base_url:
        http_client = httpx2.AsyncClient(headers={"authorization": f"Bearer {key}"}, timeout=30.0)
        async with (
            http_client,
            Client(streamable_http_client(base_url + h.settings.server.mcp_path, http_client=http_client)) as client,
        ):
            out["mcp_streamable_http"] = await via(client)
    return out


async def measure_order_overhead(h: Harness, n: int, warmup: int) -> dict[str, list[float]]:
    """place_order latency (ms), guarded vs pass-through, both direct calls against the same fake I6."""
    d = h.settings.demo
    session = await h.create_session(agent_id="latency-orders", mode="paper", known_at=None, broker="shadow")
    creds = Credentials(api_key=session["api_key"])
    wrapper = PassThroughWrapper(h.settings, h.gateway.downstream, h.clock, "latency-orders-baseline")
    window_s = h.settings.budgets.defaults.order_window_s
    state = {"i": 0}

    def side() -> Literal["buy", "sell"]:
        state["i"] += 1
        return "buy" if state["i"] % 2 else "sell"

    async def guarded() -> None:
        h.clock.advance(window_s)
        out = await h.gateway.place_order(
            creds,
            venue=d.venue,
            symbol=d.symbol,
            side=side(),
            type="market",
            qty=1.0,
            price=None,
            strategy_id="latency",
            rationale="latency probe",
        )
        if isinstance(out, RefusalBody):
            raise RuntimeError(f"order refused during latency run: {out.message}")

    async def baseline() -> None:
        await wrapper.place_order(
            venue=d.venue, symbol=d.symbol, side=side(), type="market", qty=1.0, price=None, strategy_id="latency"
        )

    return {
        "guarded": await time_calls(guarded, n, warmup),
        "baseline": await time_calls(baseline, n, warmup),
    }
