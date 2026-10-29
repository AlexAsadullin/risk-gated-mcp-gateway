"""T-Bank Invest API sandbox as a broker adapter (contract 6.8), over the REST proxy.

Calls are ``POST {base_url}{service_prefix}.<Service>/<Method>`` with a JSON body in proto3 JSON (camelCase,
int64 as strings, Quotation ``{units, nano}``), authenticated with ``Authorization: Bearer`` from ``token_env``.
Shapes follow the protobuf contracts in ``docs/tinvest-contracts`` of the umbrella repo (read 2026-10-29).

1. A gateway account is mapped to a sandbox account on first use: ``SandboxService/OpenSandboxAccount`` and one
   ``SandboxPayIn`` of ``initial_pay_in`` roubles. The mapping is kept in a JSON state file.
2. Symbols ``<SECID>-RUB-<BOARD>`` resolve to an instrument uid through ``InstrumentsService/ShareBy``, which also
   reports the lot size, the price tick and the trading status; quantities go to the broker in lots.
3. ``PostSandboxOrder`` uses the gateway's ``client_order_id`` as the idempotency key ``orderId``.

The T-Bank sandbox fills by its own documented rules (market orders at the last price whatever their size,
limit orders in full when one lot is available at the touch, a flat 0.05% fee), so it is an execution venue for
integration tests and a "testnet-style" baseline, not a model of real fills; the mirror broker is that model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp_gateway.brokers.base import BrokerUnavailable, InstrumentRules, OrderNotFound
from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import Account, Order, OrderRequest, OrderStatus, Position
from mcp_gateway.settings import ServiceEndpoint, TBankSandboxSettings

STATUS: dict[str, OrderStatus] = {
    "EXECUTION_REPORT_STATUS_FILL": "filled",
    "EXECUTION_REPORT_STATUS_REJECTED": "rejected",
    "EXECUTION_REPORT_STATUS_CANCELLED": "cancelled",
    "EXECUTION_REPORT_STATUS_NEW": "accepted",
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "partially_filled",
}
TIME_IN_FORCE = {
    "GTC": "TIME_IN_FORCE_DAY",
    "POST_ONLY": "TIME_IN_FORCE_DAY",
    "IOC": "TIME_IN_FORCE_FILL_AND_KILL",
    "FOK": "TIME_IN_FORCE_FILL_OR_KILL",
}


class TBankConfigError(BrokerUnavailable):
    """The adapter cannot run (no token, or a symbol it cannot map)."""

    def __init__(self, reason: str) -> None:
        super().__init__("tbank-sandbox", reason)


def quotation(value: float) -> dict[str, Any]:
    units = int(value)
    return {"units": str(units), "nano": round((value - units) * 1e9)}


def from_quotation(q: dict[str, Any] | None) -> float | None:
    if not q:
        return None
    return int(q.get("units", 0) or 0) + int(q.get("nano", 0) or 0) / 1e9


def split_symbol(symbol: str) -> tuple[str, str]:
    """``SBER-RUB-TQBR`` -> (``SBER``, ``TQBR``)."""
    parts = symbol.split("-")
    if len(parts) != 3:
        raise TBankConfigError(f"not a MOEX symbol: {symbol!r}")
    return parts[0], parts[2]


class TBankSandboxBroker:
    name = "tbank-sandbox"
    routed_to = "tbank-sandbox"

    def __init__(self, cfg: TBankSandboxSettings, state_path: Path | None, client: ServiceClient | None = None) -> None:
        self._cfg = cfg
        self._client = client or ServiceClient(ServiceEndpoint(base_url=cfg.base_url, timeout_s=cfg.timeout_s))
        self._client.service = "tbank-sandbox"
        self._state_path = state_path
        self._accounts: dict[str, str] = {}
        self._order_accounts: dict[str, str] = {}
        self._shares: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._state_path is not None and self._state_path.exists():
            data = json.loads(self._state_path.read_text())
            self._accounts = dict(data.get("accounts", {}))
            self._order_accounts = dict(data.get("orders", {}))

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accounts": self._accounts, "orders": self._order_accounts}, sort_keys=True))
        os.replace(tmp, self._state_path)

    def _headers(self) -> dict[str, str]:
        token = os.environ.get(self._cfg.token_env, "").strip()
        if not token:
            raise TBankConfigError(f"${self._cfg.token_env} is not set")
        return {"Authorization": f"Bearer {token}"}

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.request(
            "POST", f"{self._cfg.service_prefix}.{method}", json=body, headers=self._headers()
        )
        data: dict[str, Any] = response.json()
        return data

    async def _sandbox_account(self, account_id: str) -> str:
        if account_id in self._accounts:
            return self._accounts[account_id]
        opened = await self._call("SandboxService/OpenSandboxAccount", {"name": account_id})
        sandbox_id = str(opened["accountId"])
        await self._call(
            "SandboxService/SandboxPayIn",
            {
                "accountId": sandbox_id,
                "amount": {"currency": self._cfg.currency, **quotation(self._cfg.initial_pay_in)},
            },
        )
        self._accounts[account_id] = sandbox_id
        self._save()
        return sandbox_id

    async def _share(self, symbol: str) -> dict[str, Any]:
        if symbol not in self._shares:
            ticker, board = split_symbol(symbol)
            data = await self._call(
                "InstrumentsService/ShareBy", {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": board, "id": ticker}
            )
            self._shares[symbol] = dict(data.get("instrument") or {})
        return self._shares[symbol]

    def _symbol_of_uid(self, uid: str | None, figi: str | None) -> str | None:
        for symbol, share in self._shares.items():
            if (uid and share.get("uid") == uid) or (figi and share.get("figi") == figi):
                return symbol
        return None

    def _order(self, data: dict[str, Any], request: OrderRequest | None, lot: float) -> Order:
        lots_requested = int(data.get("lotsRequested", 0) or 0)
        lots_executed = int(data.get("lotsExecuted", 0) or 0)
        status = STATUS.get(str(data.get("executionReportStatus")), "accepted")
        executed = from_quotation(data.get("executedOrderPrice"))
        return Order(
            order_id=str(data.get("orderId", "")),
            status=status,
            reject_reason=data.get("message") or None if status == "rejected" else None,
            client_order_id=request.client_order_id if request else data.get("orderRequestId"),
            venue=request.venue if request else None,
            symbol=request.symbol if request else None,
            side=request.side if request else None,
            qty=lots_requested * lot,
            price=request.price if request else None,
            account_id=request.account_id if request else None,
            filled_qty=lots_executed * lot,
            avg_price=executed / (lots_executed * lot) if executed and lots_executed else None,
            fee=from_quotation(data.get("executedCommission")),
            broker=self.name,
        )

    async def place(self, request: OrderRequest, *, known_at: str | None) -> Order:
        share = await self._share(request.symbol)
        lot = float(share.get("lot") or 1)
        sandbox_id = await self._sandbox_account(request.account_id)
        body: dict[str, Any] = {
            "quantity": str(round(request.qty / lot)),
            "direction": "ORDER_DIRECTION_BUY" if request.side == "buy" else "ORDER_DIRECTION_SELL",
            "accountId": sandbox_id,
            "orderType": "ORDER_TYPE_LIMIT" if request.type == "limit" else "ORDER_TYPE_MARKET",
            "orderId": request.client_order_id,
            "instrumentId": share.get("uid"),
            "timeInForce": TIME_IN_FORCE[request.time_in_force],
        }
        if request.type == "limit" and request.price is not None:
            body["price"] = quotation(request.price)
        data = await self._call("SandboxService/PostSandboxOrder", body)
        order = self._order(data, request, lot)
        self._order_accounts[order.order_id] = sandbox_id
        self._save()
        return order

    def _account_of(self, order_id: str) -> str:
        account = self._order_accounts.get(order_id)
        if account is None:
            raise OrderNotFound(order_id)
        return account

    async def get(self, order_id: str, *, known_at: str | None = None) -> Order:
        data = await self._call(
            "SandboxService/GetSandboxOrderState", {"accountId": self._account_of(order_id), "orderId": order_id}
        )
        return self._order(data, None, 1.0)

    async def cancel(self, order_id: str, *, known_at: str | None = None) -> Order:
        account = self._account_of(order_id)
        await self._call("SandboxService/CancelSandboxOrder", {"accountId": account, "orderId": order_id})
        return await self.get(order_id)

    async def _portfolio(self, account_id: str) -> dict[str, Any]:
        sandbox_id = await self._sandbox_account(account_id)
        return await self._call("SandboxService/GetSandboxPortfolio", {"accountId": sandbox_id, "currency": "RUB"})

    async def positions(self, account_id: str, *, known_at: str | None) -> list[Position]:
        data = await self._portfolio(account_id)
        out: list[Position] = []
        for p in data.get("positions", []):
            if p.get("instrumentType") == "currency":
                continue
            symbol = self._symbol_of_uid(p.get("instrumentUid"), p.get("figi")) or str(p.get("ticker") or p.get("figi"))
            qty = from_quotation(p.get("quantity")) or 0.0
            mark = from_quotation(p.get("currentPrice"))
            out.append(
                Position(
                    venue="moex-tbank",
                    symbol=symbol,
                    qty=qty,
                    avg_price=from_quotation(p.get("averagePositionPrice")),
                    mark_price=mark,
                    notional=None if mark is None else qty * mark,
                )
            )
        return out

    async def account(self, account_id: str, *, known_at: str | None) -> Account:
        data = await self._portfolio(account_id)
        return Account(
            account_id=account_id,
            equity=from_quotation(data.get("totalAmountPortfolio")),
            balance=from_quotation(data.get("totalAmountCurrencies")),
            currency=self._cfg.currency,
            broker=self.name,
        )

    async def instrument(self, venue: str, symbol: str, *, known_at: str | None = None) -> InstrumentRules | None:
        share = await self._share(symbol)
        if not share:
            return None
        status = str(share.get("tradingStatus", ""))
        return InstrumentRules(
            venue=venue,
            symbol=symbol,
            lot_size=float(share.get("lot") or 1),
            tick=from_quotation(share.get("minPriceIncrement")) or 0.0,
            currency=str(share.get("currency", self._cfg.currency)).upper(),
            trading_status=status or None,
            tradable=status in ("", "SECURITY_TRADING_STATUS_NORMAL_TRADING"),
            source="tbank:ShareBy",
        )

    async def aclose(self) -> None:
        await self._client.aclose()
