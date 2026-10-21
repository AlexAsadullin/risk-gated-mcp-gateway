"""Forecast (S1), backtest (I5) and leakage-probe (S7) clients, contract 6.5."""

from __future__ import annotations

from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import (
    BacktestRequest,
    BacktestResponse,
    ForecastRequest,
    ForecastResponse,
    LeakageReport,
    ProbeRequest,
)


class ForecastClient(ServiceClient):
    service = "forecast"

    async def forecast(self, request: ForecastRequest) -> ForecastResponse:
        response = await self.request("POST", "/v1/forecast", json=request.model_dump(mode="json"))
        return ForecastResponse.model_validate(response.json())


class BacktestClient(ServiceClient):
    service = "backtest"

    async def run(self, request: BacktestRequest) -> BacktestResponse:
        response = await self.request("POST", "/v1/backtests", json=request.model_dump(mode="json"))
        return BacktestResponse.model_validate(response.json())


class LeakageClient(ServiceClient):
    service = "leakage"

    async def probe(self, request: ProbeRequest) -> LeakageReport:
        response = await self.request("POST", "/v1/probe", json=request.model_dump(mode="json"))
        return LeakageReport.model_validate(response.json())
