"""Structured refusals: the one way a tool says "no".

A refusal travels to the agent as an MCP tool error (``isError: true``) whose ``structuredContent`` is a
:class:`RefusalBody`, so an agent can branch on ``code`` instead of parsing prose.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_gateway.contracts import SCHEMA_VERSION


class RefusalCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    SESSION_FROZEN = "session_frozen"
    SESSION_CLOSED = "session_closed"
    INVALID_REQUEST = "invalid_request"
    TIME_TRAVEL = "time_travel"
    MODE_FORBIDDEN = "mode_forbidden"
    LIVE_DISABLED = "live_disabled"
    LIVE_GATE_FAILED = "live_gate_failed"
    PRE_TRADE_REJECTED = "pre_trade_rejected"
    BUDGET_BREACHED = "budget_breached"
    DOWNSTREAM_UNAVAILABLE = "downstream_unavailable"
    DOWNSTREAM_ERROR = "downstream_error"
    NOT_FOUND = "not_found"


class RefusalBody(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    refused: Literal[True] = True
    code: RefusalCode
    message: str
    tool: str | None = None
    session_status: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Refusal(Exception):
    """Raised anywhere in the gateway to refuse the current tool call.

    ``freeze`` asks the session wrapper to freeze the session after recording the refusal.
    """

    def __init__(
        self,
        code: RefusalCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        freeze: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.freeze = freeze

    def body(self, *, tool: str | None, session_status: str | None) -> RefusalBody:
        return RefusalBody(
            code=self.code,
            message=self.message,
            tool=tool,
            session_status=session_status,
            details=self.details,
        )
