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
    assert s.admin_api_key is not None
    assert s.admin_api_key.get_secret_value() == "k"
    assert s.storage.sessions_db == "other.sqlite3"
    assert s.storage.audit_dir == "audit"


def test_on_breach_must_cover_every_budget(settings: Settings) -> None:
    data = settings.budgets.model_dump()
    data["on_breach"].pop("tokens")
    with pytest.raises(ValidationError):
        BudgetSettings.model_validate(data)


def test_wilson_reference_values() -> None:
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0)
    assert lo == pytest.approx(0.7225, abs=1e-4)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_quantiles_and_bootstrap() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert median(values) == 2.5
    assert quantile(values, 1.0) == 4.0
    with pytest.raises(ValueError):
        quantile([], 0.5)
    ci1 = bootstrap_ci(values, median, n_resamples=200, seed=1)
    assert ci1 == bootstrap_ci(values, median, n_resamples=200, seed=1)
    assert 1.0 <= ci1[0] <= ci1[1] <= 4.0
    lo, hi = bootstrap_diff_ci([10.0, 11.0, 12.0], values, median, n_resamples=200, seed=2)
    assert 6.0 <= lo <= hi <= 11.0


def test_fisher_exact_reference_values() -> None:
    from mcp_gateway.bench.stats import fisher_exact_two_sided

    # Fisher's tea-tasting table [[3, 1], [1, 3]]: two-sided p = 34/70.
    assert fisher_exact_two_sided(3, 1, 1, 3) == pytest.approx(34 / 70)
    assert fisher_exact_two_sided(5, 0, 0, 5) == pytest.approx(2 / 252)
    assert fisher_exact_two_sided(1, 1, 1, 1) == pytest.approx(1.0)


def test_price_table_matches_published_rates(settings: Settings) -> None:
    """Anthropic first-party rates (USD per million tokens) checked on 2026-10-23; see README."""
    prices = {m: (p.input_per_mtok_usd, p.output_per_mtok_usd) for m, p in settings.pricing.models.items()}
    assert prices["claude-opus-5-5"] == (4.0, 20.0)
    assert prices["claude-sonnet-5-5"] == (2.0, 10.0)
    assert prices["claude-haiku-5-5"] == (0.10, 0.50)
    assert prices["claude-sonnet-4-6"] == (3.0, 15.0)
    assert prices["claude-haiku-4-5"] == (1.0, 5.0)
    assert "claude-sonnet-4-5" not in prices
