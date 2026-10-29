"""Agent sessions and their durable store.

A session binds one agent to a mode, a clock and a set of budget limits. It is persisted in SQLite under the
data root so the HTTP server, a stdio server and the admin API all see the same state. Agent API keys are
stored only as SHA-256 hashes; the keys are 256-bit random tokens, so a fast hash is sufficient (no
password-style key stretching is needed for high-entropy secrets).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from mcp_gateway.settings import BudgetKind, BudgetLimits, Mode


class SessionStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class FreezeInfo(BaseModel):
    code: str
    budget: BudgetKind | None = None
    message: str
    at: str


class Usage(BaseModel):
    """Everything the gateway accounts for in a session. Monetary values are float64 analytics, not ledgers."""

    tokens_in: int = 0
    tokens_out: int = 0
    token_cost_usd: float = 0.0
    latency_total_ms: float = 0.0
    latency_max_ms: float = 0.0
    decisions: int = 0
    order_times_ns: list[int] = Field(default_factory=list)
    orders_submitted: int = 0
    peak_equity: float | None = None
    last_equity: float | None = None
    models_used: list[str] = Field(default_factory=list)
    symbols_touched: list[str] = Field(default_factory=list)
    strategies: list[str] = Field(default_factory=list)
    data_start_ns: int | None = None
    data_end_ns: int | None = None
    backtests: int = 0
    trials_logged: int = 0
    trials_inboxed: int = 0
    refusals: dict[str, int] = Field(default_factory=dict)

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    def touch_symbols(self, symbols: list[str]) -> None:
        for symbol in symbols:
            if symbol not in self.symbols_touched:
                self.symbols_touched.append(symbol)

    def touch_window(self, start_ns: int, end_ns: int) -> None:
        self.data_start_ns = start_ns if self.data_start_ns is None else min(self.data_start_ns, start_ns)
        self.data_end_ns = end_ns if self.data_end_ns is None else max(self.data_end_ns, end_ns)

    def add_strategy(self, strategy_id: str) -> None:
        if strategy_id not in self.strategies:
            self.strategies.append(strategy_id)

    def add_model(self, model: str) -> None:
        if model not in self.models_used:
            self.models_used.append(model)

    def count_refusal(self, code: str) -> None:
        self.refusals[code] = self.refusals.get(code, 0) + 1


class Session(BaseModel):
    session_id: str
    agent_id: str
    mode: Mode
    known_at_ns: int | None
    account_id: str
    limits: BudgetLimits
    status: SessionStatus = SessionStatus.ACTIVE
    freeze: FreezeInfo | None = None
    created_at_ns: int
    api_key_hash: str
    usage: Usage = Field(default_factory=Usage)
    broker: str | None = None  # contract 6.8 adapter; None = the mode's default (sessions before 2026-10-29)


def generate_api_key(n_bytes: int) -> str:
    return secrets.token_urlsafe(n_bytes)


def hash_api_key(api_key: str) -> str:
    return "sha256:" + hashlib.sha256(api_key.encode()).hexdigest()


def keys_equal(supplied: str, expected: str) -> bool:
    """Constant-time comparison for the admin key."""
    return hmac.compare_digest(supplied.encode(), expected.encode())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    api_key_hash TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    body TEXT NOT NULL
)
"""


class SessionStore:
    """SQLite-backed session store. Each call opens a short connection, so it is safe across processes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, api_key_hash, agent_id, body) VALUES (?, ?, ?, ?)",
                (session.session_id, session.api_key_hash, session.agent_id, session.model_dump_json()),
            )

    def save(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET body = ? WHERE session_id = ?",
                (session.model_dump_json(), session.session_id),
            )

    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute("SELECT body FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return None if row is None else Session.model_validate_json(row[0])

    def get_by_key_hash(self, api_key_hash: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute("SELECT body FROM sessions WHERE api_key_hash = ?", (api_key_hash,)).fetchone()
        return None if row is None else Session.model_validate_json(row[0])

    def list(self, agent_id: str | None = None) -> list[Session]:
        with self._connect() as conn:
            if agent_id is None:
                rows = conn.execute("SELECT body FROM sessions ORDER BY rowid").fetchall()
            else:
                rows = conn.execute(
                    "SELECT body FROM sessions WHERE agent_id = ? ORDER BY rowid", (agent_id,)
                ).fetchall()
        return [Session.model_validate_json(row[0]) for row in rows]
