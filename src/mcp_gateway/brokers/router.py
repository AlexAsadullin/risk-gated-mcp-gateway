"""Choose the broker adapter of a session (contract 6.8) from settings; the gateway's only order entry point."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mcp_gateway.brokers.base import BrokerAdapter, ConfiguredRules, HttpOrderBroker
from mcp_gateway.brokers.mirror import MarketSource, MirrorBroker, PitMarketSource
from mcp_gateway.brokers.tbank_sandbox import TBankSandboxBroker
from mcp_gateway.downstream import Downstream
from mcp_gateway.settings import BrokerSettings, Mode

KNOWN = ("mirror", "shadow", "execution", "tbank-sandbox")


class UnknownBroker(ValueError):
    pass


class BrokerRouter:
    """Builds every configured adapter once; ``for_mode`` returns the default or the session's chosen one."""

    def __init__(
        self,
        cfg: BrokerSettings,
        downstream: Downstream,
        state_dir: Path | None,
        clock_ns: Callable[[], int],
        market: MarketSource | None = None,
    ) -> None:
        for name in (cfg.paper, cfg.live, *cfg.allowed_paper):
            if name not in KNOWN:
                raise UnknownBroker(f"unknown broker {name!r}; known: {', '.join(KNOWN)}")
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
        )
        self._adapters: dict[str, BrokerAdapter] = {
            "shadow": HttpOrderBroker("shadow", "i6", downstream.shadow, self.rules),
            "execution": HttpOrderBroker("execution", "i7", downstream.execution, self.rules),
            "mirror": self.mirror,
            "tbank-sandbox": TBankSandboxBroker(cfg.tbank_sandbox, state(cfg.tbank_sandbox.state_file)),
        }

    def get(self, name: str) -> BrokerAdapter:
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
        if mode is Mode.LIVE and name != self.cfg.live:
            raise UnknownBroker(f"live sessions use {self.cfg.live!r}")
        if mode is Mode.PAPER and name not in self.cfg.allowed_paper:
            raise UnknownBroker(f"paper sessions may use {', '.join(self.cfg.allowed_paper)}")
        return name

    async def aclose(self) -> None:
        for name in ("mirror", "tbank-sandbox"):
            await self._adapters[name].aclose()
