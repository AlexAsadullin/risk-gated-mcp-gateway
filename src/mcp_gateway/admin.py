"""Admin HTTP API (contract 6.6): create sessions and move their clock. Guarded by the admin key from env."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mcp_gateway.admin_models import (
    AdminError,
    CreateSessionRequest,
    CreateSessionResponse,
    SessionView,
    SetClockRequest,
)
from mcp_gateway.audit import AuditEntry
from mcp_gateway.gateway import AdminRejection, Gateway
from mcp_gateway.sessions import SessionStatus


def admin_rejection_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AdminRejection)
    body = AdminError(code=str(exc.code), message=exc.message)
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


def build_admin_router(gateway: Gateway) -> APIRouter:
    auth = gateway.settings.auth

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if gateway.settings.admin_api_key is None:
            raise HTTPException(status_code=503, detail="admin API disabled: GATEWAY_ADMIN_API_KEY is not set")
        token = None
        if authorization:
            scheme, _, value = authorization.partition(" ")
            token = value.strip() if scheme.lower() == auth.bearer_scheme else None
        if not gateway.is_admin_key(token):
            raise HTTPException(status_code=401, detail="invalid admin key")

    router = APIRouter(prefix=gateway.settings.server.admin_prefix, dependencies=[Depends(require_admin)])

    @router.post("/sessions", status_code=201)
    async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
        return gateway.create_session(request)

    @router.get("/sessions")
    async def list_sessions(agent_id: str | None = None) -> list[SessionView]:
        return gateway.list_sessions(agent_id)

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> SessionView:
        return gateway.get_session(session_id)

    @router.post("/sessions/{session_id}/clock")
    async def set_clock(session_id: str, request: SetClockRequest) -> SessionView:
        return await gateway.set_clock(session_id, request.known_at)

    @router.post("/sessions/{session_id}/unfreeze")
    async def unfreeze(session_id: str) -> SessionView:
        return await gateway.set_status(session_id, SessionStatus.ACTIVE)

    @router.post("/sessions/{session_id}/close")
    async def close(session_id: str) -> SessionView:
        return await gateway.set_status(session_id, SessionStatus.CLOSED)

    @router.get("/sessions/{session_id}/audit")
    async def audit(session_id: str) -> list[AuditEntry]:
        gateway.get_session(session_id)
        return gateway.audit.read(session_id)

    return router
