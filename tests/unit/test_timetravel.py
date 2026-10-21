from datetime import UTC, datetime

import pytest

from mcp_gateway.refusals import Refusal, RefusalCode
from mcp_gateway.settings import TimeTravelPolicy
from mcp_gateway.timetravel import drop_future_rows, guard_window
from mcp_gateway.timeutil import to_ns

KNOWN = datetime(2025, 6, 1, tzinfo=UTC)


def guard(start: str, end: str, policy: TimeTravelPolicy = TimeTravelPolicy.REFUSE, days: float = 30.0):  # type: ignore[no-untyped-def]
    return guard_window(start, end, known_at=KNOWN, policy=policy, max_window_days=days)


def test_window_before_clock_passes() -> None:
    w = guard("2025-05-31", "2025-06-01T00:00:00Z")
    assert not w.clamped
    assert w.end == KNOWN


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        ("2025-05-31", "2025-06-01T00:00:01Z", RefusalCode.TIME_TRAVEL),
        ("2025-06-02", "2025-06-03", RefusalCode.TIME_TRAVEL),
        ("2025-05-31", "2025-05-30", RefusalCode.INVALID_REQUEST),
        ("2025-01-01", "2025-05-31", RefusalCode.INVALID_REQUEST),
        ("yesterday", "2025-05-31", RefusalCode.INVALID_REQUEST),
    ],
)
def test_refusals(start: str, end: str, code: RefusalCode) -> None:
    with pytest.raises(Refusal) as info:
        guard(start, end)
    assert info.value.code is code


def test_clamp_policy() -> None:
    w = guard("2025-05-31", "2025-06-05", TimeTravelPolicy.CLAMP)
    assert w.clamped
    assert w.end == KNOWN


def test_drop_future_rows_handles_types() -> None:
    k = to_ns(KNOWN)
    rows = [
        {"knowledge_time": k - 1},
        {"knowledge_time": k + 1},
        {"knowledge_time": "2025-06-02T00:00:00Z"},
        {"knowledge_time": datetime(2025, 5, 1, tzinfo=UTC)},
        {"knowledge_time": "garbage"},
        {"knowledge_time": True},
        {"other": 1},
    ]
    kept, dropped = drop_future_rows(rows, k)
    assert dropped == 2
    assert len(kept) == 5
