"""Choose the broker adapter of a session (contract 6.8) from settings; the gateway's only order entry point."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mcp_gateway.brokers.base import BrokerAdapter, ConfiguredRules, HttpOrderBroker
from mcp_gateway.brokers.commission import CommissionModel
from mcp_gateway.brokers.mirror import MarketSource, MirrorBroker, PitMarketSource
from mcp_gateway.brokers.tbank_sandbox import TBankSandboxBroker
from mcp_gateway.downstream import Downstream
from mcp_gateway.settings import BrokerSettings, Mode

KNOWN = ("mirror", "shadow", "execution", "tbank-sandbox")
TBANK = "tbank-sandbox"


class UnknownBroker(ValueError):
    pass


class BrokerDisabled(UnknownBroker):
    """The adapter exists but configuration switched it off (the T-Bank sandbox, unless explicitly enabled)."""


def tbank_disabled_message() -> str:
    return (
        f"broker {TBANK!r} is disabled (brokers.tbank_sandbox.enabled is false): trading runs only in the local "
        "mirror sandbox; the T-Invest token is for market data"
    )


class BrokerRouter:
    """Builds every configured adapter once; ``for_mode`` returns the default or the session's chosen one."""

    def __init__(
        self,
        cfg: BrokerSettings,
        downstream: Downstream,
        state_dir: Path | None,
        clock_ns: Callable[[], int],
        market: MarketSource | None = None,
        zero_fee_file: Path | None = None,
    ) -> None:
        for name in (cfg.paper, cfg.live, *cfg.allowed_paper):
            if name not in KNOWN:
                raise UnknownBroker(f"unknown broker {name!r}; known: {', '.join(KNOWN)}")
        if not cfg.tbank_sandbox.enabled and TBANK in (cfg.paper, cfg.live):
            # A default must never point at T-Bank by accident: refuse to start instead of failing per order.
            raise BrokerDisabled(tbank_disabled_message())
        self.cfg = cfg
        self.rules = ConfiguredRules(cfg.instruments)

        def state(name: str) -> Path | None:
            return None if state_dir is None else state_dir / name

        self.mirror = MirrorBroker(
            market or PitMarketSource(downstream.pit, cfg.mirror),
            self.rules,
            cfg.mirror,
            state(cfg.mirror.state_file),
            clock_ns,
            CommissionModel(cfg.mirror.commission, zero_fee_file),
        )
        self._adapters: dict[str, BrokerAdapter] = {
            "shadow": HttpOrderBroker("shadow", "i6", downstream.shadow, self.rules),
            "execution": HttpOrderBroker("execution", "i7", downstream.execution, self.rules),
            "mirror": self.mirror,
        }
        if cfg.tbank_sandbox.enabled:
            self._adapters[TBANK] = TBankSandboxBroker(cfg.tbank_sandbox, state(cfg.tbank_sandbox.state_file))

    def get(self, name: str) -> BrokerAdapter:
        if name == TBANK and not self.cfg.tbank_sandbox.enabled:
            raise BrokerDisabled(tbank_disabled_message())
        try:
            return self._adapters[name]
        except KeyError:
            raise UnknownBroker(f"unknown broker {name!r}") from None

    def default_for(self, mode: Mode) -> str:
        return self.cfg.live if mode is Mode.LIVE else self.cfg.paper

    def check_choice(self, mode: Mode, name: str | None) -> str:
        """The broker a new session will use; a paper session may pick any of ``allowed_paper``."""
        if name is None:
            return self.default_for(mode)
        if name == TBANK and not self.cfg.tbank_sandbox.enabled:
            raise BrokerDisabled(tbank_disabled_message())
        if mode is Mode.LIVE and name != self.cfg.live:
            raise UnknownBroker(f"live sessions use {self.cfg.live!r}")
        if mode is Mode.PAPER and name not in self.cfg.allowed_paper:
            raise UnknownBroker(f"paper sessions may use {', '.join(self.cfg.allowed_paper)}")
        return name

    async def aclose(self) -> None:
        for name in ("mirror", TBANK):
            if name in self._adapters:
                await self._adapters[name].aclose()
