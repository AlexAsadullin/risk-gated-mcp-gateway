"""The bundle of downstream clients the gateway talks to, built from settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from mcp_gateway.clients.ledger import LedgerClient
from mcp_gateway.clients.orders import OrderClient
from mcp_gateway.clients.pit import PitClient
from mcp_gateway.clients.research import BacktestClient, ForecastClient, LeakageClient
from mcp_gateway.settings import ServicesSettings

SERVICE_NAMES = ("ledger", "pit", "forecast", "backtest", "leakage", "shadow", "execution")


@dataclass
class Downstream:
    ledger: LedgerClient
    pit: PitClient
    forecast: ForecastClient
    backtest: BacktestClient
    leakage: LeakageClient
    shadow: OrderClient
    execution: OrderClient

    async def aclose(self) -> None:
        for client in (
            self.ledger,
            self.pit,
            self.forecast,
            self.backtest,
            self.leakage,
            self.shadow,
            self.execution,
        ):
            await client.aclose()


def build_downstream(
    services: ServicesSettings, transports: Mapping[str, httpx.AsyncBaseTransport] | None = None
) -> Downstream:
    """Create every client. ``transports`` maps service names to in-process transports (fakes, tests)."""
    t = dict(transports or {})
    unknown = set(t) - set(SERVICE_NAMES)
    if unknown:
        raise ValueError(f"unknown services in transports: {sorted(unknown)}")
    return Downstream(
        ledger=LedgerClient(services.ledger, transport=t.get("ledger")),
        pit=PitClient(services.pit, transport=t.get("pit")),
        forecast=ForecastClient(services.forecast, transport=t.get("forecast")),
        backtest=BacktestClient(services.backtest, transport=t.get("backtest")),
        leakage=LeakageClient(services.leakage, transport=t.get("leakage")),
        shadow=OrderClient("shadow", services.shadow, transport=t.get("shadow")),
        execution=OrderClient("execution", services.execution, transport=t.get("execution")),
    )
