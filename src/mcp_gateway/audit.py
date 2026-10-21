"""Append-only, hash-chained audit log.

One JSON object per line in ``<audit_dir>/<prefix>-<yyyymmdd>.jsonl``. Each entry stores the SHA-256 of the
previous entry (``prev_hash``) and of itself (``hash``, over the canonical JSON of all other fields), so any
edit, deletion or reordering inside a file is detectable with :func:`verify_chain`. Appends hold an exclusive
``flock`` so the HTTP and stdio servers can share one log.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from mcp_gateway.contracts import SCHEMA_VERSION
from mcp_gateway.timeutil import to_iso

GENESIS_PREFIX = "genesis:"


class Decision(StrEnum):
    ALLOWED = "allowed"
    REFUSED = "refused"
    ADMIN = "admin"


class AuditEntry(BaseModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    seq: int
    ts: str
    actor: str
    session_id: str | None
    mode: str | None
    action: str
    decision: Decision
    code: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str = ""


def _digest(entry: AuditEntry) -> str:
    payload = entry.model_dump(mode="json", exclude={"hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _last_line(handle: Any) -> str | None:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return None
    block = 4096
    data = b""
    pos = size
    while pos > 0:
        step = min(block, pos)
        pos -= step
        handle.seek(pos)
        data = handle.read(step) + data
        if data.rstrip(b"\n").count(b"\n") >= 1:
            break
    lines = data.rstrip(b"\n").split(b"\n")
    return lines[-1].decode()


class AuditLog:
    def __init__(self, directory: Path, prefix: str) -> None:
        self._dir = directory
        self._prefix = prefix
        directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, ts: datetime) -> Path:
        return self._dir / f"{self._prefix}-{ts.strftime('%Y%m%d')}.jsonl"

    def files(self) -> list[Path]:
        return sorted(self._dir.glob(f"{self._prefix}-*.jsonl"))

    def append(
        self,
        *,
        ts: datetime,
        actor: str,
        session_id: str | None,
        mode: str | None,
        action: str,
        decision: Decision,
        code: str | None = None,
        args: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> AuditEntry:
        path = self.path_for(ts)
        with path.open("ab+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                last = _last_line(handle)
                if last is None:
                    seq, prev_hash = 0, GENESIS_PREFIX + path.name
                else:
                    previous = json.loads(last)
                    seq, prev_hash = int(previous["seq"]) + 1, str(previous["hash"])
                entry = AuditEntry(
                    seq=seq,
                    ts=to_iso(ts),
                    actor=actor,
                    session_id=session_id,
                    mode=mode,
                    action=action,
                    decision=decision,
                    code=code,
                    args=args or {},
                    outcome=outcome or {},
                    prev_hash=prev_hash,
                )
                entry.hash = _digest(entry)
                handle.seek(0, os.SEEK_END)
                handle.write((entry.model_dump_json() + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return entry

    def read(self, session_id: str | None = None) -> list[AuditEntry]:
        entries: list[AuditEntry] = []
        for path in self.files():
            for line in path.read_text().splitlines():
                entry = AuditEntry.model_validate_json(line)
                if session_id is None or entry.session_id == session_id:
                    entries.append(entry)
        return entries


def verify_chain(path: Path) -> int | None:
    """Return the index of the first corrupt line in ``path``, or ``None`` if the chain is intact."""
    prev = GENESIS_PREFIX + path.name
    for index, line in enumerate(path.read_text().splitlines()):
        entry = AuditEntry.model_validate_json(line)
        if entry.seq != index or entry.prev_hash != prev or entry.hash != _digest(entry):
            return index
        prev = entry.hash
    return None
