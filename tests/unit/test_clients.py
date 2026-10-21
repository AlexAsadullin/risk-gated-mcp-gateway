"""Downstream clients against respx mocks of contracts 6.1, 6.3, 6.4 and 6.5."""

import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pyarrow as pa
import pytest
import respx

from mcp_gateway.clients.base import DownstreamError, DownstreamUnavailable
from mcp_gateway.clients.pit import PitLeakage, decode_rows
from mcp_gateway.contracts import (
    Actor,
    BacktestRequest,
    ForecastRequest,
    LeakageEvent,
    Order,
    OrderRequest,
    PitQuery,
    ProbeRequest,
    TrialRecord,
)
from mcp_gateway.downstream import build_downstream
from mcp_gateway.settings import LedgerSettings, Settings
from mcp_gateway.timeutil import ManualClock
from mcp_gateway.trials import Destination, TrialLogger, build_trial, ledger_strategy_id

VERDICT = {
    "strategy_id": "a/s", "n_trials": 3, "effective_trials": 2.5, "sharpe": 1.2, "psr": 0.9, "dsr": 0.8,
    "pbo": None, "min_trl": 2.0, "passed": False, "reasons": ["dsr"], "thresholds": {}, "leakage_flags": [],
    "computed_at": "2025-06-01T00:00:00Z", "schema_version": "1",
}
QUERY = PitQuery(dataset="tape.trades", start="2025-01-01T00:00:00Z", end="2025-01-02T00:00:00Z",
                 known_at="2025-01-02T00:00:00Z", mode="research")


def order_request() -> OrderRequest:
    return OrderRequest(client_order_id="c1", account_id="acct", venue="binance", symbol="BTC-USDT-PERP",
                        side="buy", type="market", qty=1.0, time_in_force="GTC", strategy_id="a/s",
                        actor=Actor(kind="agent", id="a"))


@respx.mock
async def test_ledger_client(settings: Settings) -> None:
    ds = build_downstream(settings.services)
    base = settings.services.ledger.base_url
    respx.post(f"{base}/v1/trials").respond(200, json={"trial_id": "t1", "schema_version": "1"})
    respx.post(f"{base}/v1/leakage").respond(200, json={})
    route = respx.get(f"{base}/v1/strategies/a%2Fs/verdict").respond(200, json=VERDICT)
    record = build_trial(settings=settings.ledger, kind="backtest", agent_id="a", strategy_id="a/s", params={},
                         metrics={}, tags=[], started_at=datetime(2025, 1, 1, tzinfo=UTC),
                         finished_at=datetime(2025, 1, 1, tzinfo=UTC))
    assert (await ds.ledger.post_trial(record)).trial_id == "t1"
    await ds.ledger.post_leakage(LeakageEvent(source="s7", severity="block", kind="k", observed_at="x"))
    verdict = await ds.ledger.verdict("a/s")
    assert route.called and verdict.effective_trials == 2.5 and not verdict.passed
    await ds.aclose()


@respx.mock
async def test_pit_client_json_arrow_and_conflict(settings: Settings) -> None:
    ds = build_downstream(settings.services)
    url = f"{settings.services.pit.base_url}/v1/query"
    respx.post(url).mock(side_effect=[
        httpx.Response(200, json={"rows": [{"a": 1}]}, headers={"X-Dataset-Fingerprint": "sha256:ab"}),
        httpx.Response(409, json={"source": "i2", "kind": "end_after_known_at"}),
    ])
    result = await ds.pit.query(QUERY)
    assert result.rows == [{"a": 1}] and result.fingerprint == "sha256:ab"
    assert respx.calls[0].request.url.params["format"] == "json"
    with pytest.raises(PitLeakage) as info:
        await ds.pit.query(QUERY)
    assert info.value.event["kind"] == "end_after_known_at"

    sink = io.BytesIO()
    table = pa.table({"price": [1.0, 2.0]})
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    respx.post(url).respond(200, content=sink.getvalue(), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert (await ds.pit.query(QUERY)).rows == [{"price": 1.0}, {"price": 2.0}]
    await ds.aclose()


def test_decode_rows_shapes() -> None:
    assert decode_rows("application/json", b"", [{"a": 1}]) == [{"a": 1}]
    assert decode_rows("application/json", b"", {"columns": ["a", "b"], "data": [[1, 2]]}) == [{"a": 1, "b": 2}]
    with pytest.raises(ValueError):
        decode_rows("application/json", b"", {"weird": True})


@respx.mock
async def test_research_and_order_clients(settings: Settings) -> None:
    ds = build_downstream(settings.services)
    sv = settings.services
    respx.post(f"{sv.forecast.base_url}/v1/forecast").respond(200, json={
        "forecasts": [{"symbol": "X", "horizon": 1, "mean": 0.01, "quantiles": {"0.5": 0.01}}], "model_cutoff": None})
    respx.post(f"{sv.backtest.base_url}/v1/backtests").respond(200, json={"run_id": "r1", "trial_id": None, "metrics": {}})
    respx.post(f"{sv.leakage.base_url}/v1/probe").respond(200, json={"model_id": "m", "window": {}, "flagged": True})
    order = {"order_id": "o1", "status": "accepted", "reject_reason": None}
    respx.post(f"{sv.shadow.base_url}/v1/orders").respond(200, json=order)
    respx.get(f"{sv.shadow.base_url}/v1/orders/o1").respond(200, json=order)
    respx.delete(f"{sv.shadow.base_url}/v1/orders/o1").respond(200, json=order | {"status": "cancelled"})
    respx.get(f"{sv.shadow.base_url}/v1/accounts/acct").respond(200, json={"account_id": "acct", "equity": 5.0})
    respx.get(f"{sv.shadow.base_url}/v1/positions").mock(side_effect=[
        httpx.Response(200, json=[{"venue": "v", "symbol": "s", "qty": 1.0}]),
        httpx.Response(200, json={"positions": []}),
        httpx.Response(200, json={"bad": 1}),
    ])
    f = await ds.forecast.forecast(ForecastRequest(model="m", symbols=["X"], horizon=1, known_at="k", quantiles=[0.5]))
    assert f.forecasts[0].mean == 0.01
    b = await ds.backtest.run(BacktestRequest(strategy="s", params={}, universe=["X"], start="a", end="b",
                                              fidelity="event", known_at="k"))
    assert b.run_id == "r1"
    assert (await ds.leakage.probe(ProbeRequest(model_id="m", entities=[], start="a", end="b", languages=[]))).flagged
    assert (await ds.shadow.place(order_request())).order_id == "o1"
    assert (await ds.shadow.get("o1")).status == "accepted"
    assert (await ds.shadow.cancel("o1")).status == "cancelled"
    assert (await ds.shadow.account("acct")).equity == 5.0
    assert len(await ds.shadow.positions("acct")) == 1
    assert await ds.shadow.positions("acct") == []
    with pytest.raises(ValueError):
        await ds.shadow.positions("acct")
    await ds.aclose()


@respx.mock
async def test_error_mapping(settings: Settings) -> None:
    ds = build_downstream(settings.services)
    url = f"{settings.services.backtest.base_url}/v1/backtests"
    req = BacktestRequest(strategy="s", params={}, universe=["X"], start="a", end="b", fidelity="event", known_at="k")
    respx.post(url).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(503), httpx.Response(422, text="bad")])
    with pytest.raises(DownstreamUnavailable):
        await ds.backtest.run(req)
    with pytest.raises(DownstreamUnavailable):
        await ds.backtest.run(req)
    with pytest.raises(DownstreamError) as info:
        await ds.backtest.run(req)
    assert info.value.status_code == 422 and info.value.body == "bad"
    await ds.aclose()
    with pytest.raises(ValueError):
        build_downstream(settings.services, {"nope": httpx.MockTransport(lambda r: httpx.Response(200))})


@respx.mock
async def test_trial_logger_inbox_fallback(settings: Settings, tmp_path: Path) -> None:
    ds = build_downstream(settings.services)
    respx.post(f"{settings.services.ledger.base_url}/v1/trials").mock(side_effect=httpx.ConnectError("down"))
    clock = ManualClock(datetime(2025, 6, 1, tzinfo=UTC))
    logger = TrialLogger(ds.ledger, settings.ledger, tmp_path / "inbox", clock)
    record = build_trial(settings=settings.ledger, kind="order-intent", agent_id="a", strategy_id="a/s",
                         params={"x": 1}, metrics={}, tags=["t"], started_at=clock.now(), finished_at=clock.now())
    logged = await logger.log(record)
    assert logged.destination is Destination.INBOX
    path = tmp_path / "inbox" / "i9-20250601.jsonl"
    assert TrialRecord.model_validate_json(path.read_text().splitlines()[0]) == record
    assert record.source == "i9" and record.tags == ["order-intent", "t"] and record.schema_version == "1"
    await ds.aclose()


def test_strategy_id_mapping() -> None:
    template = LedgerSettings(source="i9", inbox_relpath="x", family_prefix="p",
                              strategy_id_template="{agent_id}/{strategy}", periods_per_year=1.0).strategy_id_template
    assert ledger_strategy_id(template, "a", "mom") == "a/mom"
    assert ledger_strategy_id(template, "a", "a/mom") == "a/mom"


def test_contract_roundtrip_keeps_extras() -> None:
    raw = {"order_id": "o", "status": "filled", "venue_fee": 0.1}
    order = Order.model_validate(raw)
    assert order.model_dump()["venue_fee"] == 0.1 and order.schema_version == "1"
