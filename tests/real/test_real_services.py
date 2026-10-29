"""Opt-in: the gateway against the shipped I2 (point-in-time-store) and S6 (research-ledger) services.

Run with ``make test-integration`` (``pytest -m integration``). Both services start as subprocesses from their
own repositories' ``.venv``; nothing in those repositories is modified. All data goes to a scratch
``TRADEQ_DATA_DIR`` under this repository's gitignored ``tmp/`` folder, never the shared data root. I2 is seeded
through its own CLI (``pit ingest tape.trades``) from a contract-6.2 tape file. I5, S1, S7 and I6 stay fakes.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from mcp_gateway.harness import Harness
from mcp_gateway.settings import Settings
from tests.conftest import ADMIN_KEY

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent
PIT_REPO = WORKSPACE / "point-in-time-store"
LEDGER_REPO = WORKSPACE / "research-ledger"
PIT_BIN = PIT_REPO / ".venv" / "bin"
LEDGER_BIN = LEDGER_REPO / ".venv" / "bin"
BT_REPO = WORKSPACE / "backtest-orchestrator"
BT_BIN = BT_REPO / ".venv" / "bin"
BT_STRATEGY = "sma_cross"  # registered in I5 (GET /v1/strategies)
BT_PARAMS = {"fast": 2, "slow": 5, "qty": 10.0, "seed": 3}  # the gateway moves seed to I5's own field
STARTUP_TIMEOUT_S = 60.0
KNOWN_AT = "2025-06-01T00:00:00Z"
NS = 1_000_000_000

# Runs inside I2's own interpreter (it has pyarrow); writes I1 tape files per contract 6.2 revision 2 (MOEX):
# one trade per minute, and one order-book snapshot (5 levels per side) just before the last trade.
TAPE_WRITER = """
import sys, pyarrow as pa, pyarrow.parquet as pq
from pathlib import Path
root, start_ns, n = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
base = root / "venue=moex-tbank"
def common(k, ts):
    return {"venue": ["moex-tbank"] * k, "symbol": ["SBER-RUB-TQBR"] * k,
            "ts_exchange": pa.array(ts, pa.int64()), "ts_local": pa.array([t + 1_000_000_000 for t in ts], pa.int64()),
            "capture_region": ["test"] * k, "connection_id": ["c1"] * k, "seq": pa.array([None] * k, pa.int64())}
out = base / "channel=trades/symbol=SBER-RUB-TQBR/date=2025-05-31/part-0.parquet"
out.parent.mkdir(parents=True, exist_ok=True)
ts = [start_ns + i * 60_000_000_000 for i in range(n)]
pq.write_table(pa.table({**common(n, ts),
    "trade_id": [f"t{i:05d}" for i in range(n)], "price": [280.0 + i / 100 for i in range(n)],
    "size": [10.0] * n, "side": ["buy" if i % 2 else "sell" for i in range(n)]}), out)
last = ts[-1] - 30_000_000_000
mid = 280.0 + (n - 1) / 100
bids = [("bid", round(mid - 0.01 * (k + 1), 2), k) for k in range(5)]
levels = bids + [("ask", round(mid + 0.01 * k, 2), k) for k in range(5)]
book = base / "channel=book_snapshot/symbol=SBER-RUB-TQBR/date=2025-05-31/part-0.parquet"
book.parent.mkdir(parents=True, exist_ok=True)
pq.write_table(pa.table({**common(10, [last] * 10),
    "side": [lv[0] for lv in levels], "price": [lv[1] for lv in levels], "size": [100.0] * 10,
    "level": pa.array([lv[2] for lv in levels], pa.int32()), "snapshot_id": [f"TQBR-{last}"] * 10}), book)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(url: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"service exited early ({proc.returncode}); log:\n{log.read_text()}")
        try:
            if httpx.get(url + "/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    pytest.fail(f"service at {url} did not become healthy; log:\n{log.read_text()}")


@dataclass
class RealServices:
    data_dir: Path
    pit_url: str
    ledger_url: str
    backtest_url: str


@pytest.fixture
def real_services() -> Iterator[RealServices]:
    for binary in (PIT_BIN / "pit", PIT_BIN / "python", LEDGER_BIN / "ledger", BT_BIN / "bt"):
        if not binary.exists():
            pytest.skip(f"{binary} not found; run make setup in the sibling repository")
    scratch = REPO / "tmp" / f"integration-{uuid.uuid4().hex[:8]}"
    data_dir = scratch / "data"
    data_dir.mkdir(parents=True)
    pit_port, ledger_port, bt_port = _free_port(), _free_port(), _free_port()
    ledger_url, pit_url = f"http://127.0.0.1:{ledger_port}", f"http://127.0.0.1:{pit_port}"
    bt_url = f"http://127.0.0.1:{bt_port}"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(scratch)),
        "TRADEQ_DATA_DIR": str(data_dir),
        "LEDGER_SERVICE__PORT": str(ledger_port),
        "PIT_SERVICE__PORT": str(pit_port),
        "PIT_LEDGER__BASE_URL": ledger_url,
        # I5 against the real I2 (bars aggregated from tape.trades, JSON times) and the real S6.
        "BT_SERVICE__PORT": str(bt_port),
        "BT_PIT__MODE": "http",
        "BT_PIT__BASE_URL": pit_url,
        "BT_PIT__BARS_SOURCE": "trades",
        "BT_MARKET__VENUE": "moex-tbank",
        "BT_LEDGER__MODE": "http",
        "BT_LEDGER__BASE_URL": ledger_url,
    }
    tape_root = data_dir / "market-tape-recorder" / "tape"
    start_ns = (1748736000 - 2 * 3600) * NS  # 2025-05-31T22:00:00Z, two hours before KNOWN_AT
    subprocess.run(
        [str(PIT_BIN / "python"), "-c", TAPE_WRITER, str(tape_root), str(start_ns), "120"],
        check=True,
        env=env,
        cwd=scratch,
    )
    for dataset in ("tape.trades", "tape.book_snapshot"):
        subprocess.run(
            [str(PIT_BIN / "pit"), "ingest", dataset, "--venue", "moex-tbank"],
            check=True,
            env=env,
            cwd=scratch,
            capture_output=True,
        )
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for name, cmd, url in (
            ("ledger", [str(LEDGER_BIN / "ledger"), "serve"], ledger_url),
            ("pit", [str(PIT_BIN / "pit"), "serve", "--port", str(pit_port)], pit_url),
            ("bt", [str(BT_BIN / "bt"), "serve"], bt_url),
        ):
            log = scratch / f"{name}.log"
            proc = subprocess.Popen(cmd, env=env, cwd=scratch, stdout=log.open("wb"), stderr=subprocess.STDOUT)
            procs.append(proc)
            _wait_healthy(url, proc, log)
        yield RealServices(data_dir=data_dir, pit_url=pit_url, ledger_url=ledger_url, backtest_url=bt_url)
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)
    shutil.rmtree(scratch)


def _settings(settings: Settings, svc: RealServices) -> Settings:
    services = settings.services.model_copy(
        update={
            "pit": settings.services.pit.model_copy(update={"base_url": svc.pit_url}),
            "ledger": settings.services.ledger.model_copy(update={"base_url": svc.ledger_url}),
            "backtest": settings.services.backtest.model_copy(update={"base_url": svc.backtest_url}),
        }
    )
    return settings.model_copy(update={"services": services})


async def test_research_and_paper_session_against_real_i2_and_s6(
    settings: Settings, real_services: RealServices
) -> None:
    h = Harness.create(
        _settings(settings, real_services), admin_key=ADMIN_KEY, real_services={"pit", "ledger", "backtest"}
    )
    d = h.settings.demo
    tape = {"venue": d.venue, "symbol": d.symbol, "channel": "trades"}
    research = await h.create_session(agent_id="itest", mode="research", known_at=KNOWN_AT)
    async with h.agent(research["api_key"]) as agent:
        data = await agent.call("market_data", **tape, start="2025-05-31T23:00:00Z", end=KNOWN_AT)
        assert not data.is_error, data.data
        rows = data.data["rows"]
        assert data.data["n_rows"] == 60
        assert data.data["fingerprint"].startswith("sha256:")
        assert data.data["leakage_flags"] == []
        assert all(r["knowledge_time"] <= KNOWN_AT for r in rows)
        assert rows[-1]["valid_time"].startswith("2025-05-31T23:59:00")

        leak = await agent.call("market_data", **tape, start="2025-05-31T23:00:00Z", end="2025-06-01T01:00:00Z")
        assert leak.code == "time_travel"

        bt = await agent.call(
            "run_backtest",
            strategy=BT_STRATEGY,
            params=BT_PARAMS,
            universe=[d.symbol],
            start="2025-05-31T22:00:00Z",
            end=KNOWN_AT,
            fidelity="vectorised",
        )
        assert not bt.is_error, json.dumps(bt.data)
        assert bt.data["logged_to"] == "i5"
        peek = await agent.call(
            "run_backtest",
            strategy=BT_STRATEGY,
            params=BT_PARAMS,
            universe=[d.symbol],
            start="2025-05-31T22:00:00Z",
            end="2025-06-02T00:00:00Z",
            fidelity="vectorised",
        )
        assert peek.code == "time_travel"

        verdict = await agent.call("verdict", strategy_id=BT_STRATEGY)
        assert not verdict.is_error, verdict.data
        assert verdict.data["verdict"]["strategy_id"] == f"itest:{BT_STRATEGY}"
        assert verdict.data["verdict"]["n_trials"] == 2, json.dumps(verdict.data["verdict"])
        assert not verdict.data["promotable"]
        flags = verdict.data["verdict"]["leakage_flags"]
        assert [(f["source"], f["severity"], f["kind"]) for f in flags] == [("i9", "block", "end_after_known_at")], (
            flags
        )
        async with httpx.AsyncClient(base_url=real_services.ledger_url) as ledger:
            family = (await ledger.get(f"/v1/families/{verdict.data['verdict']['family']}")).json()
        assert family["n_trials"] == 2, family
        assert family["sources"] == {"i5": 1, "i9": 1}, json.dumps(family)
        assert family["actors"] == {"agent": 2}, json.dumps(family)  # S6 counts actors by kind
        budget = await agent.call("budget")
        assert budget.data["usage"]["trials_logged"] == 2
        assert budget.data["usage"]["trials_inboxed"] == 0

    paper = await h.create_session(agent_id="itest", mode="paper", known_at=KNOWN_AT)
    async with h.agent(paper["api_key"]) as agent:
        order = await agent.call(
            "place_order",
            **{k: tape[k] for k in ("venue", "symbol")},
            side="buy",
            type="market",
            qty=10.0,
            strategy_id=BT_STRATEGY,
            rationale="integration test",
        )
        assert not order.is_error, order.data
        assert order.data["reference_price"] == pytest.approx(280.0 + 119 / 100)
        assert order.data["logged_to"] == "ledger"
        # The mirror sandbox matched against the order book stored in the real I2: the best ask at the clock.
        assert order.data["routed_to"] == "mirror"
        assert order.data["order"]["avg_price"] == pytest.approx(280.0 + 119 / 100)
        sl = await agent.call("market_slice", symbols=["SBER-RUB-TQBR"])
        assert not sl.is_error, sl.data
        (inst,) = sl.data["slice"]["instruments"]
        assert inst["best_ask"] == pytest.approx(281.19)
        assert inst["last_price"] == pytest.approx(281.19)
    assert len(h.platform.exchange.received) == 0
    inbox = real_services.data_dir / "research-ledger" / "inbox"
    assert not list(inbox.glob("i9-*.jsonl")) if inbox.exists() else True
    await h.gateway.downstream.aclose()
