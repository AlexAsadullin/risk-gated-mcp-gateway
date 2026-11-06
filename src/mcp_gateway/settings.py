"""Typed gateway settings.

Defaults come from ``config/settings.yaml``; environment variables with the ``GATEWAY_`` prefix override them
(nested delimiter ``__``, e.g. ``GATEWAY_RISK__PRICE_BAND_BPS=150``). Secrets (admin key, stdio agent key) come
only from the environment. The platform data root comes from ``TRADEQ_DATA_DIR``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

SETTINGS_FILE_ENV = "GATEWAY_SETTINGS_FILE"
_REPO_SETTINGS = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"


def settings_file() -> Path:
    """Path of the YAML defaults file: ``$GATEWAY_SETTINGS_FILE`` or the repository's ``config/settings.yaml``."""
    override = os.environ.get(SETTINGS_FILE_ENV)
    return Path(override) if override else _REPO_SETTINGS


class Mode(StrEnum):
    """Session mode. Research is time-travel pinned; paper uses ``brokers.paper`` (the mirror); live uses I7."""

    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class BreachAction(StrEnum):
    """What happens when a budget is breached: refuse the call only, or refuse and freeze the session."""

    REFUSE = "refuse"
    FREEZE = "freeze"


class ServerSettings(BaseModel):
    host: str
    port: int
    mcp_path: str
    admin_prefix: str
    json_response: bool
    stateless_http: bool
    allowed_hosts: list[str]
    allowed_origins: list[str]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class AuthSettings(BaseModel):
    api_key_header: str
    bearer_scheme: str
    session_override_header: str
    api_key_bytes: int = Field(ge=16)


class StorageSettings(BaseModel):
    subdir: str
    sessions_db: str
    audit_dir: str
    audit_file_prefix: str
    demo_dir: str
    reports_dir: str
    brokers_dir: str


class ServiceEndpoint(BaseModel):
    base_url: str
    timeout_s: float = Field(gt=0)


class ServicesSettings(BaseModel):
    ledger: ServiceEndpoint
    pit: ServiceEndpoint
    forecast: ServiceEndpoint
    backtest: ServiceEndpoint
    leakage: ServiceEndpoint
    shadow: ServiceEndpoint
    execution: ServiceEndpoint


class LedgerSettings(BaseModel):
    source: str
    inbox_relpath: str
    family_prefix: str
    backtest_family_template: str
    strategy_id_template: str
    periods_per_year: float = Field(gt=0)


class ClockSettings(BaseModel):
    allow_rewind: bool
    allow_future: bool


class ModesSettings(BaseModel):
    live_enabled: bool
    live_require_verdict: bool
    live_require_clean_leakage: bool


class TimeTravelPolicy(StrEnum):
    REFUSE = "refuse"
    CLAMP = "clamp"


class DataSettings(BaseModel):
    market_data_dataset_template: str
    feature_entity_column: str
    feature_step_seconds: int = Field(gt=0)
    channels: list[str]
    max_rows: int = Field(gt=0)
    max_window_days: float = Field(gt=0)
    time_travel_policy: TimeTravelPolicy


class ForecastSettings(BaseModel):
    quantiles: list[float]
    max_symbols: int = Field(gt=0)
    max_horizon: int = Field(gt=0)


class BacktestSettings(BaseModel):
    fidelities: list[str]
    max_universe: int = Field(gt=0)


class LeakageSettings(BaseModel):
    languages: list[str]
    entity_separator: str
    default_window_days: float = Field(gt=0)
    flag_event_kind: str


class BudgetLimits(BaseModel):
    """Per-agent budget limits. Monetary limits are in the account currency (quote units); USD for tokens."""

    capital: float = Field(gt=0, description="Max gross exposure after a trade, quote currency.")
    max_drawdown: float = Field(gt=0, lt=1, description="Max fractional drawdown of account equity from peak.")
    max_order_notional: float = Field(gt=0, description="Max notional of a single order, quote currency.")
    max_orders_per_window: int = Field(gt=0, description="Max orders submitted per rate window.")
    order_window_s: float = Field(gt=0, description="Length of the order-rate window, seconds.")
    max_tokens: int = Field(gt=0, description="Max total LLM tokens (in + out) reported via report_usage.")
    max_token_cost_usd: float = Field(gt=0, description="Max total LLM spend, USD, from the price table.")
    max_latency_ms: float = Field(gt=0, description="Max latency of a single reported decision, ms.")
    max_total_latency_ms: float = Field(gt=0, description="Max cumulative reported decision latency, ms.")


class BudgetOverrides(BaseModel):
    """Partial budget limits supplied by the admin when creating a session; unset fields use the defaults."""

    capital: float | None = Field(default=None, gt=0)
    max_drawdown: float | None = Field(default=None, gt=0, lt=1)
    max_order_notional: float | None = Field(default=None, gt=0)
    max_orders_per_window: int | None = Field(default=None, gt=0)
    order_window_s: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_token_cost_usd: float | None = Field(default=None, gt=0)
    max_latency_ms: float | None = Field(default=None, gt=0)
    max_total_latency_ms: float | None = Field(default=None, gt=0)

    def apply(self, defaults: BudgetLimits) -> BudgetLimits:
        return defaults.model_copy(update=self.model_dump(exclude_none=True))


class BudgetKind(StrEnum):
    CAPITAL = "capital"
    DRAWDOWN = "drawdown"
    ORDER_NOTIONAL = "order_notional"
    ORDER_RATE = "order_rate"
    TOKENS = "tokens"
    TOKEN_COST = "token_cost"
    LATENCY = "latency"
    TOTAL_LATENCY = "total_latency"


class BudgetSettings(BaseModel):
    defaults: BudgetLimits
    on_breach: dict[BudgetKind, BreachAction]
    frozen_allowed_tools: list[str]

    @model_validator(mode="after")
    def _every_kind_has_action(self) -> BudgetSettings:
        missing = set(BudgetKind) - set(self.on_breach)
        if missing:
            raise ValueError(f"budgets.on_breach is missing actions for: {sorted(missing)}")
        return self


class ModelPrice(BaseModel):
    input_per_mtok_usd: float = Field(ge=0)
    output_per_mtok_usd: float = Field(ge=0)


class UnknownModelPolicy(StrEnum):
    DEFAULT = "default"
    REFUSE = "refuse"


class PricingSettings(BaseModel):
    default: ModelPrice
    models: dict[str, ModelPrice]
    unknown_model_policy: UnknownModelPolicy


class Instrument(BaseModel):
    venue: str
    symbol: str


class ReferencePriceSettings(BaseModel):
    channel: str
    lookback_s: float = Field(gt=0)
    price_column: str


class RiskSettings(BaseModel):
    whitelist: list[Instrument]
    max_order_notional: float = Field(gt=0)
    max_position_notional: float = Field(gt=0)
    price_band_bps: float = Field(gt=0)
    require_reference_price: bool
    require_instrument_rules: bool
    reference: ReferencePriceSettings


class InstrumentRule(BaseModel):
    """Trading rules of one instrument (contract 6.8 ``InstrumentRules``), used when a broker has no live source.

    ``price_band_bps`` is the daily price band around the reference price (MOEX limits orders outside it).
    """

    venue: str
    symbol: str
    lot_size: float = Field(gt=0)
    tick: float = Field(gt=0)
    currency: str
    price_band_bps: float | None = Field(default=None, gt=0)


class CommissionSettings(BaseModel):
    """Broker commission of the mirror sandbox: a fraction of each fill's trade value, on buys and sells alike.

    ``rate`` applies to every instrument except the zero-fee set, which pays ``zero_fee_rate``. The set is the
    union of ``zero_fee_instruments`` (tickers, symbols, FIGIs or instrument uids) and, when the file exists, the
    funds listed under ``zero_fee_reference_key`` in ``zero_fee_reference_file`` (relative to
    ``TRADEQ_DATA_DIR``; an empty string disables it). Each fill's fee is rounded half-up to ``rounding``.
    """

    source: str
    currency: str
    rate: float = Field(ge=0, lt=1)
    zero_fee_rate: float = Field(ge=0, lt=1)
    rounding: float = Field(gt=0)
    zero_fee_instruments: list[str]
    zero_fee_reference_file: str
    zero_fee_reference_key: str


class MirrorSettings(BaseModel):
    """The built-in sandbox that matches orders against the real order book read from I2 (contract 6.8)."""

    initial_cash: float = Field(gt=0)
    currency: str
    commission: CommissionSettings
    book_dataset: str
    trades_dataset: str
    book_lookback_s: float = Field(gt=0)
    max_book_age_s: float = Field(gt=0)
    state_file: str


class TBankSandboxSettings(BaseModel):
    """T-Bank Invest API sandbox over its REST proxy; the token comes only from ``token_env``.

    Off by default (``enabled: false``): trading runs in the local mirror sandbox only, and the T-Invest token is
    for market-data download (I1). While disabled, no session can choose this adapter.
    """

    enabled: bool = False
    base_url: str
    service_prefix: str
    token_env: str
    timeout_s: float = Field(gt=0)
    initial_pay_in: float = Field(gt=0)
    currency: str
    state_file: str


class BrokerSettings(BaseModel):
    """Contract 6.8: which execution adapter serves which mode, and the instruments' trading rules.

    ``paper`` and ``live`` name the default adapter of each mode; a session may name another one in
    ``allowed_paper`` when it is created. Adapter names: ``mirror``, ``shadow`` (I6), ``execution`` (I7),
    ``tbank-sandbox``.
    """

    paper: str
    live: str
    allowed_paper: list[str]
    instruments: list[InstrumentRule]
    mirror: MirrorSettings
    tbank_sandbox: TBankSandboxSettings


class OrderSettings(BaseModel):
    account_id_template: str
    default_time_in_force: Literal["GTC", "IOC", "FOK", "POST_ONLY"]


class FakeSettings(BaseModel):
    """Parameters of the in-process fake services used by tests, the demo and the report (not production)."""

    seed: int
    bar_seconds: int = Field(gt=0)
    knowledge_delay_s: int = Field(ge=0)
    cycle_amplitude: float
    cycle_bars: int = Field(gt=0)
    noise_scale: float = Field(ge=0)
    base_prices: dict[str, float]
    book_levels: int = Field(gt=0)
    book_level_size: float = Field(gt=0)
    book_ticks: dict[str, float]
    initial_cash: float = Field(gt=0)
    currency: str
    flagged_models: list[str]
    verdict_min_sharpe: float
    verdict_max_trials: int = Field(gt=0)


class DemoSettings(BaseModel):
    """Scripted demo (make reproduce) and report experiments (make report)."""

    agent_id: str
    venue: str
    symbol: str
    known_at: str
    wall_clock: str
    model: str
    report_seed: int
    report_episodes: int = Field(gt=0)
    report_latency_calls: int = Field(gt=0)
    report_latency_repeats: int = Field(gt=0)
    report_bootstrap: int = Field(gt=0)


class Settings(BaseSettings):
    """The gateway's complete configuration."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Field(validation_alias=AliasChoices("TRADEQ_DATA_DIR", "GATEWAY_DATA_DIR"))
    admin_api_key: SecretStr | None = None
    agent_api_key: SecretStr | None = None

    server: ServerSettings
    auth: AuthSettings
    storage: StorageSettings
    services: ServicesSettings
    ledger: LedgerSettings
    clock: ClockSettings
    modes: ModesSettings
    data: DataSettings
    forecast: ForecastSettings
    backtest: BacktestSettings
    leakage: LeakageSettings
    budgets: BudgetSettings
    pricing: PricingSettings
    risk: RiskSettings
    orders: OrderSettings
    brokers: BrokerSettings
    fakes: FakeSettings
    demo: DemoSettings

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=settings_file())
        return init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings

    @property
    def project_dir(self) -> Path:
        """This project's subtree of the platform data root."""
        return self.data_dir / self.storage.subdir

    @property
    def sessions_db_path(self) -> Path:
        return self.project_dir / self.storage.sessions_db

    @property
    def audit_dir(self) -> Path:
        return self.project_dir / self.storage.audit_dir

    @property
    def broker_state_dir(self) -> Path:
        """Where broker adapters keep their state (mirror accounts, T-Bank sandbox account map)."""
        return self.project_dir / self.storage.brokers_dir

    @property
    def zero_fee_reference_path(self) -> Path | None:
        """I1's list of zero-commission funds (read-only), or None when the setting is empty."""
        relpath = self.brokers.mirror.commission.zero_fee_reference_file
        return self.data_dir / relpath if relpath else None

    @property
    def demo_dir(self) -> Path:
        return self.project_dir / self.storage.demo_dir

    @property
    def reports_dir(self) -> Path:
        return self.project_dir / self.storage.reports_dir

    @property
    def ledger_inbox_dir(self) -> Path:
        """S6 offline inbox (contract 6.1), the one path outside our subtree the contract lets us write."""
        return self.data_dir / self.ledger.inbox_relpath


def load_settings(**overrides: object) -> Settings:
    """Load settings from YAML + env, with optional keyword overrides (used by tests and the demo).

    ``.env`` is also loaded into the process environment (without overriding it), because broker tokens such as
    ``TBANK_TOKEN`` are read from the variable named by ``token_env``, not from a settings field.
    """
    load_dotenv(".env", override=False)
    return Settings(**overrides)  # type: ignore[arg-type]
