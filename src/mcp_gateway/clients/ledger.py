"""Research Ledger (S6) client, contract 6.1."""

from __future__ import annotations

from urllib.parse import quote

from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import LeakageEvent, TrialAck, TrialRecord, Verdict


class LedgerClient(ServiceClient):
    service = "ledger"

    async def post_trial(self, record: TrialRecord) -> TrialAck:
        response = await self.request("POST", "/v1/trials", json=record.model_dump(mode="json"))
        return TrialAck.model_validate(response.json())

    async def post_leakage(self, event: LeakageEvent) -> None:
        await self.request("POST", "/v1/leakage", json=event.model_dump(mode="json"))

    async def verdict(self, strategy_id: str) -> Verdict:
        response = await self.request("GET", f"/v1/strategies/{quote(strategy_id, safe='')}/verdict")
        return Verdict.model_validate(response.json())
