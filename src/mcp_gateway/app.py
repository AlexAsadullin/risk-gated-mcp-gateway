"""Composition root: builds the gateway from settings and exposes it as an HTTP app (admin + MCP) or over stdio."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.admin import admin_rejection_handler, build_admin_router
from mcp_gateway.audit import AuditLog
from mcp_gateway.downstream import Downstream, build_downstream
from mcp_gateway.gateway import AdminRejection, Gateway
from mcp_gateway.mcp_server import CredentialSource, build_mcp_server, header_credentials
from mcp_gateway.sessions import SessionStore
from mcp_gateway.settings import Settings
from mcp_gateway.timeutil import Clock, SystemClock
from mcp_gateway.trials import TrialLogger


def build_gateway(
    settings: Settings,
    *,
    clock: Clock | None = None,
    transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
) -> Gateway:
    clock = clock or SystemClock()
    downstream: Downstream = build_downstream(settings.services, transports)
    return Gateway(
        settings=settings,
        store=SessionStore(settings.sessions_db_path),
        audit=AuditLog(settings.audit_dir, settings.storage.audit_file_prefix),
        downstream=downstream,
        trials=TrialLogger(downstream.ledger, settings.ledger, settings.ledger_inbox_dir, clock),
        clock=clock,
    )


class BearerRequired:
    """Reject MCP HTTP requests without an ``Authorization`` header with 401 before they reach the SDK."""

    def __init__(self, app: ASGIApp, header: str) -> None:
        self._app = app
        self._header = header.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not any(k.lower() == self._header for k, _ in scope["headers"]):
            body = b'{"schema_version":"1","refused":true,"code":"unauthenticated","message":"missing API key"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


@dataclass
class HttpApp:
    app: FastAPI
    mcp: MCPServer[Any]
    gateway: Gateway


def build_http_app(gateway: Gateway, credentials: CredentialSource | None = None) -> HttpApp:
    """FastAPI app serving the admin API under ``server.admin_prefix`` and MCP (streamable HTTP) at ``mcp_path``."""
    server_cfg = gateway.settings.server
    mcp = build_mcp_server(gateway, credentials or header_credentials(gateway.settings.auth))
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=server_cfg.mcp_path,
        json_response=server_cfg.json_response,
        stateless_http=server_cfg.stateless_http,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=server_cfg.allowed_hosts,
            allowed_origins=server_cfg.allowed_origins,
        ),
        host=server_cfg.host,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield
        await gateway.downstream.aclose()

    app = FastAPI(title="Tradeq risk-gated MCP gateway", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(AdminRejection, admin_rejection_handler)
    app.include_router(build_admin_router(gateway))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"schema_version": "1", "status": "ok"}

    app.mount("/", BearerRequired(mcp_app, gateway.settings.auth.api_key_header))
    return HttpApp(app=app, mcp=mcp, gateway=gateway)
