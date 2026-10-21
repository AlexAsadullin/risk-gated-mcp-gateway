from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_gateway.bench.stats import bootstrap_ci, bootstrap_diff_ci, median, quantile, wilson_interval
from mcp_gateway.settings import BudgetSettings, Settings, load_settings, settings_file


def test_defaults_from_yaml(settings: Settings, tmp_path: Path) -> None:
    assert settings.server.port == 8608
    assert settings.data_dir == tmp_path / "data"
    assert settings.project_dir.name == "risk-gated-mcp-gateway"
    assert settings.ledger_inbox_dir == tmp_path / "data" / "research-ledger" / "inbox"
    assert settings.admin_api_key is None
    assert settings_file().name == "settings.yaml"
    assert settings.reports_dir.parent == settings.project_dir


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_RISK__PRICE_BAND_BPS", "150")
    monkeypatch.setenv("GATEWAY_ADMIN_API_KEY", "k")
    monkeypatch.setenv("GATEWAY_STORAGE__SESSIONS_DB", "other.sqlite3")
    s = load_settings()
    assert s.risk.price_band_bps == 150.0
    assert s.admin_api_key is not None and s.admin_api_key.get_secret_value() == "k"
    assert s.storage.sessions_db == "other.sqlite3" and s.storage.audit_dir == "audit"


def test_on_breach_must_cover_every_budget(settings: Settings) -> None:
    data = settings.budgets.model_dump()
    data["on_breach"].pop("tokens")
    with pytest.raises(ValidationError):
        BudgetSettings.model_validate(data)


def test_wilson_reference_values() -> None:
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(0.7225, abs=1e-4)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_quantiles_and_bootstrap() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert median(values) == 2.5 and quantile(values, 1.0) == 4.0
    with pytest.raises(ValueError):
        quantile([], 0.5)
    ci1 = bootstrap_ci(values, median, n_resamples=200, seed=1)
    assert ci1 == bootstrap_ci(values, median, n_resamples=200, seed=1)
    assert 1.0 <= ci1[0] <= ci1[1] <= 4.0
    lo, hi = bootstrap_diff_ci([10.0, 11.0, 12.0], values, median, n_resamples=200, seed=2)
    assert 6.0 <= lo <= hi <= 11.0
