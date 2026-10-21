"""UTC time helpers: ISO-8601 on the wire, int64 nanoseconds in storage (platform convention)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

NS_PER_SECOND = 1_000_000_000
NS_PER_MS = 1_000_000
SECONDS_PER_DAY = 86_400


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp or date into an aware UTC datetime.

    Naive values (``2025-01-01`` or ``2025-01-01T12:00:00``) are taken to be UTC, which is what an LLM means
    when it omits the offset on a platform that is UTC everywhere.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """Render an aware datetime as an ISO-8601 UTC string with a ``Z`` suffix."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; use timezone-aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def to_ns(value: datetime) -> int:
    """Nanoseconds since the Unix epoch (UTC). Exact: computed from integer components, not float seconds."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; use timezone-aware UTC")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * SECONDS_PER_DAY + delta.seconds) * NS_PER_SECOND + delta.microseconds * 1_000


def from_ns(ns: int) -> datetime:
    """Inverse of :func:`to_ns` at microsecond resolution (datetime cannot hold nanoseconds)."""
    seconds, rem = divmod(ns, NS_PER_SECOND)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds, microseconds=rem // 1_000)


def ns_to_iso(ns: int) -> str:
    return to_iso(from_ns(ns))


class Clock(Protocol):
    """Wall-clock source, injected so tests and the demo are deterministic."""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to; used by tests and the offline demo."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock needs an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        self._now = value
