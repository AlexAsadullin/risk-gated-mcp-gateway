"""The T-Invest commission model: 0.25% per fill, 0% for T-Capital funds, rounded half-up to the kopek."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mcp_gateway.brokers.commission import CommissionModel, ticker_of
from mcp_gateway.settings import CommissionSettings, Settings

UID = "e8acd2fb-6de6-4ea4-9bfb-0daad9b2ed7b"


def cfg(**kw: object) -> CommissionSettings:
    base: dict[str, object] = {
        "source": "test",
        "currency": "RUB",
        "rate": 0.0025,
        "zero_fee_rate": 0.0,
        "rounding": 0.01,
        "zero_fee_instruments": ["TMOS"],
        "zero_fee_reference_file": "",
        "zero_fee_reference_key": "funds",
    }
    return CommissionSettings(**(base | kw))  # type: ignore[arg-type]


def fund(ticker: str, uid: str = "", figi: str = "") -> dict[str, object]:
    return {
        "ticker": ticker,
        "figi": figi,
        "instrument_uid": uid,
        "class_code": "TQTF",
        "name": ticker,
        "lot": 1,
        "currency": "rub",
    }


def test_configured_rates_are_the_t_invest_tariff(settings: Settings) -> None:
    c = settings.brokers.mirror.commission
    assert (c.rate, c.zero_fee_rate, c.currency, c.rounding) == (0.0025, 0.0, "RUB", 0.01)
    assert "T-Invest" in c.source
    assert settings.zero_fee_reference_path == (
        settings.data_dir / "market-tape-recorder/reference/tbank_zero_fee_funds.json"
    )


def test_ordinary_stock_pays_quarter_percent() -> None:
    m = CommissionModel(cfg())
    assert m.fee("SBER-RUB-TQBR", 300.0, 100.0) == 75.0
    assert m.fee("LKOH-RUB-TQBR", 6900.5, 3.0) == 51.75  # 20,701.50 * 0.25% = 51.75375
    assert m.fee("SBER-RUB-TQBR", 300.0, -100.0) == 75.0  # sign of a sell never makes the fee negative


def test_rounding_is_half_up_to_the_kopek() -> None:
    m = CommissionModel(cfg())
    assert m.fee("SBER-RUB-TQBR", 1.0, 2.0) == 0.01  # 0.005 -> 0.01 (half-up; float maths would give 0.0)
    assert m.fee("SBER-RUB-TQBR", 0.5, 1.0) == 0.0  # 0.00125 -> 0.00
    assert m.fee("SBER-RUB-TQBR", 285.01, 10.0) == 7.13  # 7.12525
    assert m.fee("SBER-RUB-TQBR", 285.01, 50.0) == 35.63  # 35.62625


def test_zero_fee_by_ticker_or_symbol() -> None:
    m = CommissionModel(cfg(zero_fee_instruments=["tmos", "TGLD-RUB-TQTF"]))
    assert ticker_of("TMOS-RUB-TQTF") == "TMOS"
    assert m.fee("TMOS-RUB-TQTF", 7.5, 1000.0) == 0.0
    assert m.fee("TGLD-RUB-TQTF", 10.0, 1000.0) == 0.0
    assert m.fee("SBER-RUB-TQBR", 10.0, 1000.0) == 25.0


def test_reference_file_adds_funds_and_resolves_uids(tmp_path: Path) -> None:
    path = tmp_path / "tbank_zero_fee_funds.json"
    payload = {"source": "t-invest", "retrieved_at": "2026-11-05T00:00:00Z", "rule": "T-Capital funds"}
    path.write_text(json.dumps(payload | {"funds": [fund("TRUR", uid=UID), fund("TPAY", figi="BBG000TPAY01")]}))
    m = CommissionModel(cfg(zero_fee_instruments=[UID, "BBG000TPAY01"]), path)
    assert m.zero_fee_keys() >= {"TRUR", "TPAY"}
    assert m.fee("TRUR-RUB-TQTF", 8.0, 100.0) == 0.0
    assert m.fee("SBER-RUB-TQBR", 8.0, 100.0) == 2.0


def test_missing_reference_file_falls_back_to_config_and_is_picked_up_later(tmp_path: Path) -> None:
    path = tmp_path / "tbank_zero_fee_funds.json"
    m = CommissionModel(cfg(), path)
    with capture_logs() as logs:
        assert m.fee("TMOS-RUB-TQTF", 7.0, 10.0) == 0.0
        assert m.fee("TRUR-RUB-TQTF", 8.0, 100.0) == 2.0
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["zero_fee_reference_missing"]
    path.write_text(json.dumps({"funds": [fund("TRUR")]}))
    assert m.fee("TRUR-RUB-TQTF", 8.0, 100.0) == 0.0


def test_reference_file_without_funds_key_or_broken_warns(tmp_path: Path) -> None:
    path = tmp_path / "tbank_zero_fee_funds.json"
    path.write_text(json.dumps({"source": "x", "items": [fund("TRUR")]}))
    m = CommissionModel(cfg(), path)
    with capture_logs() as logs:
        assert m.fee("TRUR-RUB-TQTF", 8.0, 100.0) == 2.0
        assert m.fee("TMOS-RUB-TQTF", 8.0, 100.0) == 0.0  # the config list still applies
    assert "zero_fee_reference_without_list" in [e["event"] for e in logs]
    path.write_text("{not json")
    os.utime(path, (1, 1))  # a new mtime makes the model re-read the file
    with capture_logs() as logs:
        assert m.fee("TRUR-RUB-TQTF", 8.0, 100.0) == 2.0
    assert "zero_fee_reference_unreadable" in [e["event"] for e in logs]


def test_unresolved_uid_is_warned_and_ignored() -> None:
    m = CommissionModel(cfg(zero_fee_instruments=[UID]))
    with capture_logs() as logs:
        assert m.fee("SBER-RUB-TQBR", 100.0, 1.0) == 0.25
    assert [e["event"] for e in logs] == ["zero_fee_uid_unresolved"]


@pytest.mark.parametrize("rate", [0.003, 0.0005])
def test_rate_comes_from_config(rate: float) -> None:
    assert CommissionModel(cfg(rate=rate)).fee("SBER-RUB-TQBR", 100.0, 100.0) == pytest.approx(10_000 * rate)
