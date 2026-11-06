"""Broker commission of the mirror sandbox: the T-Invest tariff applied to every simulated fill.

The fee of one fill is ``round_half_up(price * qty * rate, rounding)`` in roubles, charged on buys and sells
alike and the same for taker and maker fills. ``rate`` is the basic tariff (0.25% of the trade value);
instruments in the zero-fee set (T-Bank's own funds, managed by T-Capital) pay ``zero_fee_rate`` (0%).
The arithmetic is done in ``decimal.Decimal`` so that the per-fill rounding to the kopek is exact, as the
platform conventions ask for fees; the result is stored as float64 like the rest of the mirror's cash.

The zero-fee set is the union of the configured list and the reference file that I1 writes
(``$TRADEQ_DATA_DIR/market-tape-recorder/reference/tbank_zero_fee_funds.json``: a top-level list of funds with
``ticker``, ``figi``, ``instrument_uid``, ``class_code``, ...). The file is optional: while it is missing or
unreadable the configured list is used and a warning is logged. It is re-read when its modification time changes,
so a file that appears while the gateway runs is picked up without a restart.
"""

from __future__ import annotations

import json
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import structlog

from mcp_gateway.settings import CommissionSettings

log = structlog.get_logger(__name__)

SYMBOL_SEPARATOR = "-"  # symbols are <SECID>-RUB-<BOARD> (contract 6.2)


def ticker_of(symbol: str) -> str:
    """The exchange ticker (SECID) of a platform symbol: ``TMOS-RUB-TQTF`` -> ``TMOS``."""
    return symbol.split(SYMBOL_SEPARATOR, 1)[0].upper()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class CommissionModel:
    """Fee per fill from :class:`CommissionSettings` plus the optional zero-fee reference file."""

    def __init__(self, cfg: CommissionSettings, reference_file: Path | None = None) -> None:
        self.cfg = cfg
        self._rate = Decimal(str(cfg.rate))
        self._zero_rate = Decimal(str(cfg.zero_fee_rate))
        self._step = Decimal(str(cfg.rounding))
        self._reference_file = reference_file
        self._reference_mtime: float | None = None
        self._reference: list[dict[str, Any]] = []
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ zero-fee set

    def _warn_once(self, once: str, event: str, **kw: Any) -> None:
        if once not in self._warned:
            self._warned.add(once)
            log.warning(event, **kw)

    def _refresh_reference(self) -> None:
        path = self._reference_file
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._reference, self._reference_mtime = [], None
            self._warn_once(
                f"missing:{path}", "zero_fee_reference_missing", path=str(path), fallback="config zero_fee_instruments"
            )
            return
        if mtime == self._reference_mtime:
            return
        self._reference_mtime = mtime
        self._reference = []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            self._warn_once(f"bad:{path}:{mtime}", "zero_fee_reference_unreadable", path=str(path), error=str(exc))
            return
        key = self.cfg.zero_fee_reference_key
        funds = data.get(key) if isinstance(data, dict) else None
        if not isinstance(funds, list):
            self._warn_once(
                f"nokey:{path}:{mtime}",
                "zero_fee_reference_without_list",
                path=str(path),
                key=key,
                fallback="config zero_fee_instruments",
            )
            return
        self._reference = [f for f in funds if isinstance(f, dict) and isinstance(f.get("ticker"), str)]
        log.info("zero_fee_reference_loaded", path=str(path), funds=len(self._reference), source=data.get("source"))

    def zero_fee_keys(self) -> frozenset[str]:
        """Upper-cased tickers and symbols that pay ``zero_fee_rate``."""
        self._refresh_reference()
        by_id = {
            str(f[field]).upper(): f["ticker"]
            for f in self._reference
            for field in ("instrument_uid", "figi")
            if f.get(field)
        }
        keys = {str(f["ticker"]).upper() for f in self._reference}
        for entry in self.cfg.zero_fee_instruments:
            item = entry.strip().upper()
            if item in by_id:
                keys.add(str(by_id[item]).upper())
            elif _is_uuid(item):
                # An instrument uid (UUID) that no reference fund resolves to a ticker: it cannot match a symbol.
                self._warn_once(f"uid:{item}", "zero_fee_uid_unresolved", entry=entry)
            else:
                keys.add(item)
        return frozenset(keys)

    def is_zero_fee(self, symbol: str) -> bool:
        keys = self.zero_fee_keys()
        return symbol.upper() in keys or ticker_of(symbol) in keys

    # ------------------------------------------------------------------ fees

    def rate(self, symbol: str) -> Decimal:
        return self._zero_rate if self.is_zero_fee(symbol) else self._rate

    def fee(self, symbol: str, price: float, qty: float) -> float:
        """Commission of one fill in roubles, rounded half-up to ``rounding``; never negative."""
        value = abs(Decimal(str(price)) * Decimal(str(qty)))
        return float((value * self.rate(symbol)).quantize(self._step, rounding=ROUND_HALF_UP))
