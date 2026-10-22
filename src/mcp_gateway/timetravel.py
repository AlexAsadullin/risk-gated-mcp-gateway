"""Time-travel enforcement: requested windows are checked against the session clock before any downstream call.

The point-in-time store (I2) enforces the same rule; the gateway checks first so a leaking request never leaves
the gateway, and then also filters any returned row whose ``knowledge_time`` is after ``known_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mcp_gateway.refusals import Refusal, RefusalCode
from mcp_gateway.settings import TimeTravelPolicy
from mcp_gateway.timeutil import parse_iso, to_iso, to_ns

KNOWLEDGE_TIME_COLUMN = "knowledge_time"
VALID_TIME_COLUMN = "valid_time"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    clamped: bool


def parse_window(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        return parse_iso(start), parse_iso(end)
    except ValueError as exc:
        raise Refusal(
            RefusalCode.INVALID_REQUEST,
            f"start and end must be ISO-8601 timestamps (UTC), e.g. 2025-01-01T00:00:00Z: {exc}",
        ) from exc


def guard_window(
    start: str,
    end: str,
    *,
    known_at: datetime,
    policy: TimeTravelPolicy,
    max_window_days: float,
) -> Window:
    """Validate ``[start, end]`` against ``known_at``; refuse (or clamp, by policy) anything after it."""
    start_dt, end_dt = parse_window(start, end)
    if start_dt >= end_dt:
        raise Refusal(RefusalCode.INVALID_REQUEST, "start must be before end")
    if end_dt - start_dt > timedelta(days=max_window_days):
        raise Refusal(
            RefusalCode.INVALID_REQUEST,
            f"window longer than {max_window_days:g} days; split the request",
            details={"max_window_days": max_window_days},
        )
    details = {
        "requested_start": to_iso(start_dt),
        "requested_end": to_iso(end_dt),
        "known_at": to_iso(known_at),
    }
    if start_dt >= known_at:
        raise Refusal(
            RefusalCode.TIME_TRAVEL,
            "the whole window is after the session clock; data from the future is not available",
            details=details,
        )
    if end_dt > known_at:
        if policy is TimeTravelPolicy.CLAMP:
            return Window(start=start_dt, end=known_at, clamped=True)
        raise Refusal(
            RefusalCode.TIME_TRAVEL,
            "end is after the session clock (known_at); request end <= known_at",
            details=details,
        )
    return Window(start=start_dt, end=end_dt, clamped=False)


def _knowledge_ns(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return to_ns(value)
    if isinstance(value, str):
        try:
            return to_ns(parse_iso(value))
        except ValueError:
            return None
    return None


def drop_future_rows(rows: list[dict[str, Any]], known_at_ns: int) -> tuple[list[dict[str, Any]], int]:
    """Remove rows whose ``knowledge_time`` is after ``known_at`` (defence in depth); return rows and count."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        kt = _knowledge_ns(row.get(KNOWLEDGE_TIME_COLUMN))
        if kt is not None and kt > known_at_ns:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def sort_by_valid_time(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows by ``valid_time`` (ISO string or int ns); I2 orders by key columns, not by time.

    Rows without a parseable valid time keep their relative order at the front (stable sort).
    """
    return sorted(rows, key=lambda row: _knowledge_ns(row.get(VALID_TIME_COLUMN)) or 0)
