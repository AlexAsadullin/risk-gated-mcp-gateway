from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_gateway.timeutil import ManualClock, SystemClock, from_ns, ns_to_iso, parse_iso, to_iso, to_ns


def test_parse_iso_variants() -> None:
    assert parse_iso("2025-01-01") == datetime(2025, 1, 1, tzinfo=UTC)
    assert parse_iso("2025-01-01T12:00:00Z") == datetime(2025, 1, 1, 12, tzinfo=UTC)
    assert parse_iso("2025-01-01T15:00:00+03:00") == datetime(2025, 1, 1, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_iso("not a date")


def test_to_iso_and_naive_rejected() -> None:
    assert to_iso(datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=1)))) == "2024-12-31T23:00:00Z"
    with pytest.raises(ValueError):
        to_iso(datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        to_ns(datetime(2025, 1, 1))


def test_ns_known_value() -> None:
    assert to_ns(datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)) == 1_000_000_000
    assert ns_to_iso(1_500_000_000) == "1970-01-01T00:00:01.500000Z"


@given(st.datetimes(min_value=datetime(1971, 1, 1), max_value=datetime(2200, 1, 1), timezones=st.just(UTC)))
def test_ns_roundtrip(value: datetime) -> None:
    assert from_ns(to_ns(value)) == value


def test_clocks() -> None:
    assert SystemClock().now().tzinfo is not None
    clock = ManualClock(datetime(2025, 1, 1, tzinfo=UTC))
    clock.advance(60)
    assert clock.now() == datetime(2025, 1, 1, 0, 1, tzinfo=UTC)
    clock.set(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now().year == 2026
    with pytest.raises(ValueError):
        ManualClock(datetime(2025, 1, 1))
