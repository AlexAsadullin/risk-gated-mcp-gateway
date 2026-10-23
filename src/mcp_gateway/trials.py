"""Trial capture: every agent backtest and order intent becomes a ``TrialRecord`` in the Research Ledger (S6).

If S6 cannot take the record, it is appended to the ledger's offline inbox (contract 6.1), so no idea is lost
and the ledger ingests it later. Records of refused attempts are kept too: an idea the gateway refused is still
an idea the agent tried, and hiding it would understate the number of trials behind any later winner.
"""

from __future__ import annotations

import fcntl
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from mcp_gateway.clients.base import DownstreamError, DownstreamUnavailable
from mcp_gateway.clients.ledger import LedgerClient
from mcp_gateway.contracts import Actor, LeakageEvent, TrialRecord
from mcp_gateway.settings import LedgerSettings
from mcp_gateway.timeutil import Clock, to_iso

log = structlog.get_logger(__name__)


class Destination(StrEnum):
    LEDGER = "ledger"
    INBOX = "inbox"
    I5 = "i5"  # the backtest service logged the trial in S6 itself, under the gateway's strategy id


class LoggedTrial(BaseModel):
    trial_id: str
    destination: Destination
    reason: str | None = None


def ledger_strategy_id(template: str, agent_id: str, strategy: str) -> str:
    """Map an agent-facing strategy name to its ledger id (idempotent for ids that are already mapped)."""
    mapped = template.format(agent_id=agent_id, strategy=strategy)
    prefix = template.format(agent_id=agent_id, strategy="")
    if prefix and strategy.startswith(prefix):
        return strategy
    return mapped


def build_trial(
    *,
    settings: LedgerSettings,
    kind: str,
    agent_id: str,
    strategy_id: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    tags: list[str],
    started_at: datetime,
    finished_at: datetime,
    seed: int | None = None,
    dataset_fingerprint: str | None = None,
    family: str | None = None,
) -> TrialRecord:
    """Build a contract-6.1 trial record for an agent idea of a given ``kind`` (``backtest`` or ``order-intent``).

    The gateway never sees the per-period return series (I5 keeps it), so ``returns`` is null here; when I5
    logged its own trial, its id is carried in ``params.linked_trial_id`` so S6 can join the two.
    """
    return TrialRecord(
        trial_id=str(uuid.uuid4()),
        family=family or f"{settings.family_prefix}/{agent_id}/{kind}/{strategy_id}",
        strategy_id=strategy_id,
        source=settings.source,
        actor=Actor(kind="agent", id=agent_id),
        params=params,
        dataset_fingerprint=dataset_fingerprint,
        code_version=None,
        seed=seed,
        started_at=to_iso(started_at),
        finished_at=to_iso(finished_at),
        periods_per_year=settings.periods_per_year,
        returns=None,
        metrics=metrics,
        tags=[kind, *tags],
    )


class TrialLogger:
    def __init__(self, ledger: LedgerClient, settings: LedgerSettings, inbox_dir: Path, clock: Clock) -> None:
        self._ledger = ledger
        self._settings = settings
        self._inbox_dir = inbox_dir
        self._clock = clock

    def inbox_path(self) -> Path:
        return self._inbox_dir / f"{self._settings.source}-{self._clock.now().strftime('%Y%m%d')}.jsonl"

    async def log(self, record: TrialRecord) -> LoggedTrial:
        try:
            ack = await self._ledger.post_trial(record)
        except (DownstreamUnavailable, DownstreamError) as exc:
            self._append_inbox(record)
            log.info("trial_inboxed", trial_id=record.trial_id, reason=str(exc))
            return LoggedTrial(trial_id=record.trial_id, destination=Destination.INBOX, reason=str(exc))
        return LoggedTrial(trial_id=ack.trial_id, destination=Destination.LEDGER)

    async def report_leakage(self, event: LeakageEvent) -> Destination:
        """Post a look-ahead attempt to S6; S6's inbox takes leakage events in the same files as trials."""
        try:
            await self._ledger.post_leakage(event)
        except (DownstreamUnavailable, DownstreamError) as exc:
            self._append_inbox(event)
            log.info("leakage_event_inboxed", kind=event.kind, reason=str(exc))
            return Destination.INBOX
        return Destination.LEDGER

    def _append_inbox(self, record: TrialRecord | LeakageEvent) -> None:
        path = self.inbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(record.model_dump_json(exclude_none=False) + "\n")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
