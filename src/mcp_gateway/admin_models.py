"""Wire models for the admin HTTP API (contract 6.6), used by operators and the Agent Arena (S8)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_gateway.contracts import SCHEMA_VERSION
from mcp_gateway.sessions import FreezeInfo, Session, SessionStatus, Usage
from mcp_gateway.settings import BudgetLimits, BudgetOverrides, Mode
from mcp_gateway.timeutil import ns_to_iso


class CreateSessionRequest(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")
    mode: Mode
    known_at: str | None = Field(
        default=None, description="Session clock (ISO-8601). Required in research mode; null = wall clock."
    )
    budgets: BudgetOverrides = Field(default_factory=BudgetOverrides)
    account_id: str | None = Field(
        default=None, description="Optional extension: I6/I7 account; defaults to orders.account_id_template."
    )


class SetClockRequest(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    known_at: str


class SessionView(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    session_id: str
    agent_id: str
    mode: Mode
    known_at: str | None
    account_id: str
    status: SessionStatus
    freeze: FreezeInfo | None
    limits: BudgetLimits
    usage: Usage
    created_at: str

    @classmethod
    def of(cls, session: Session) -> SessionView:
        return cls(
            session_id=session.session_id,
            agent_id=session.agent_id,
            mode=session.mode,
            known_at=None if session.known_at_ns is None else ns_to_iso(session.known_at_ns),
            account_id=session.account_id,
            status=session.status,
            freeze=session.freeze,
            limits=session.limits,
            usage=session.usage,
            created_at=ns_to_iso(session.created_at_ns),
        )


class CreateSessionResponse(SessionView):
    api_key: str = Field(description="Agent credential for this session; shown once, stored only as a hash.")
    mcp_path: str


class AdminError(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
