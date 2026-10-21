"""Offline harness: the real gateway wired to the in-process fakes, for the demo, the report and tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import Client
from pydantic import SecretStr

from mcp_gateway.app import HttpApp, build_gateway, build_http_app
from mcp_gateway.fakes.services import FakePlatform
from mcp_gateway.fakes.world import world_from_settings
from mcp_gateway.gateway import Gateway
from mcp_gateway.mcp_server import build_mcp_server, fixed_credentials
from mcp_gateway.settings import Settings
from mcp_gateway.timeutil import ManualClock, parse_iso


@dataclass
class ToolReply:
    """A decoded MCP tool result."""

    is_error: bool
    data: dict[str, Any]

    @property
    def code(self) -> str | None:
        return self.data.get("code") if self.is_error else None


def decode(result: Any) -> ToolReply:
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    return ToolReply(is_error=bool(result.is_error), data=dict(data))


@dataclass
class Harness:
    settings: Settings
    clock: ManualClock
    platform: FakePlatform
    gateway: Gateway
    http: HttpApp
    admin_key: str

    @classmethod
    def create(cls, settings: Settings, *, admin_key: str, down: set[str] | None = None) -> Harness:
        settings = settings.model_copy(update={"admin_api_key": SecretStr(admin_key)})
        clock = ManualClock(parse_iso(settings.demo.wall_clock))
        platform = FakePlatform(world=world_from_settings(settings.fakes), cfg=settings.fakes, clock=clock)
        platform.down = set(down or ())
        gateway = build_gateway(settings, clock=clock, transports=platform.transports())
        return cls(settings, clock, platform, gateway, build_http_app(gateway), admin_key)

    def admin_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.http.app),
            base_url="http://127.0.0.1",
            headers={"authorization": f"Bearer {self.admin_key}"},
        )

    async def create_session(self, **body: Any) -> dict[str, Any]:
        async with self.admin_client() as client:
            response = await client.post(f"{self.settings.server.admin_prefix}/sessions", json=body)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data

    async def set_clock(self, session_id: str, known_at: str) -> httpx.Response:
        async with self.admin_client() as client:
            return await client.post(
                f"{self.settings.server.admin_prefix}/sessions/{session_id}/clock", json={"known_at": known_at}
            )

    @asynccontextmanager
    async def agent(self, api_key: str) -> AsyncIterator[AgentConnection]:
        """An in-process MCP client bound to one session's API key."""
        server = build_mcp_server(self.gateway, fixed_credentials(api_key))
        async with Client(server) as client:
            yield AgentConnection(client)


class AgentConnection:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def call(self, tool: str, **arguments: Any) -> ToolReply:
        return decode(await self.client.call_tool(tool, arguments))
