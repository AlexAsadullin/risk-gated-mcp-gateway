import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp_gateway.audit import AuditLog, Decision, verify_chain
from mcp_gateway.sessions import Session, SessionStore, Usage, generate_api_key, hash_api_key, keys_equal
from mcp_gateway.settings import Mode, Settings

T0 = datetime(2025, 6, 1, tzinfo=UTC)


def test_audit_chain_and_tamper_detection(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit", "audit")
    for i in range(5):
        log.append(
            ts=T0,
            actor="a",
            session_id="s1" if i % 2 else "s2",
            mode="research",
            action=f"t{i}",
            decision=Decision.ALLOWED,
            args={"i": i},
        )
    log.append(
        ts=T0 + timedelta(days=1),
        actor="a",
        session_id="s1",
        mode=None,
        action="next",
        decision=Decision.REFUSED,
        code="x",
    )
    files = log.files()
    assert len(files) == 2
    assert all(verify_chain(p) is None for p in files)
    assert len(log.read("s1")) == 3
    assert len(log.read()) == 6
    lines = files[0].read_text().splitlines()
    entry = json.loads(lines[2])
    entry["args"] = {"i": 99}
    lines[2] = json.dumps(entry)
    files[0].write_text("\n".join(lines) + "\n")
    assert verify_chain(files[0]) == 2


def test_audit_long_lines(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, "audit")
    for _ in range(3):
        log.append(
            ts=T0,
            actor="a",
            session_id=None,
            mode=None,
            action="big",
            decision=Decision.ADMIN,
            args={"blob": "x" * 10_000},
        )
    assert verify_chain(log.files()[0]) is None


def test_keys() -> None:
    key = generate_api_key(32)
    assert len(key) >= 40
    assert hash_api_key(key).startswith("sha256:")
    assert hash_api_key(key) != key
    assert keys_equal(key, key)
    assert not keys_equal(key, key + "x")


def test_store_roundtrip(tmp_path: Path, settings: Settings) -> None:
    store = SessionStore(tmp_path / "db" / "s.sqlite3")
    session = Session(
        session_id="s1",
        agent_id="a",
        mode=Mode.PAPER,
        known_at_ns=None,
        account_id="acct",
        limits=settings.budgets.defaults,
        created_at_ns=0,
        api_key_hash="h1",
    )
    store.create(session)
    session.usage = Usage(tokens_in=5)
    store.save(session)
    assert store.get("s1") == session
    assert store.get_by_key_hash("h1") == session
    assert store.get("nope") is None
    assert store.get_by_key_hash("nope") is None
    assert [s.session_id for s in store.list("a")] == ["s1"]
    assert store.list("b") == []
    assert len(store.list()) == 1


def test_usage_helpers() -> None:
    u = Usage()
    u.touch_symbols(["A", "A", "B"])
    u.touch_window(5, 10)
    u.touch_window(2, 8)
    u.add_model("m")
    u.add_model("m")
    u.add_strategy("s")
    u.add_strategy("s")
    u.count_refusal("x")
    u.count_refusal("x")
    assert u.symbols_touched == ["A", "B"]
    assert (u.data_start_ns, u.data_end_ns) == (2, 10)
    assert u.models_used == ["m"]
    assert u.strategies == ["s"]
    assert u.refusals == {"x": 2}
