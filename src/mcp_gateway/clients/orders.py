"""Order API client (contract 6.4), shared by the Shadow Exchange (I6) and the execution engine (I7)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import Account, Order, OrderRequest, Position
from mcp_gateway.settings import ServiceEndpoint


def _items(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return list(payload[key])
    raise ValueError(f"expected a list or an object with '{key}'")


class OrderClient(ServiceClient):
    def __init__(self, service: str, endpoint: ServiceEndpoint, **kwargs: Any) -> None:
        super().__init__(endpoint, **kwargs)
        self.service = service

    async def place(self, request: OrderRequest) -> Order:
        response = await self.request("POST", "/v1/orders", json=request.model_dump(mode="json"))
        return Order.model_validate(response.json())

    async def get(self, order_id: str) -> Order:
        response = await self.request("GET", f"/v1/orders/{quote(order_id, safe='')}")
        return Order.model_validate(response.json())

    async def cancel(self, order_id: str) -> Order:
        response = await self.request("DELETE", f"/v1/orders/{quote(order_id, safe='')}")
        return Order.model_validate(response.json())

    async def account(self, account_id: str) -> Account:
        response = await self.request("GET", f"/v1/accounts/{quote(account_id, safe='')}")
        return Account.model_validate(response.json())

    async def positions(self, account_id: str) -> list[Position]:
        response = await self.request("GET", "/v1/positions", params={"account_id": account_id})
        return [Position.model_validate(item) for item in _items(response.json(), "positions")]
