"""Point-in-time Store (I2) client, contract 6.3."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import PitQuery, PitResult

ARROW_STREAM = "application/vnd.apache.arrow.stream"
FINGERPRINT_HEADER = "X-Dataset-Fingerprint"
HTTP_CONFLICT = 409


class PitLeakage(Exception):
    """I2 refused the query with 409 and a LeakageEvent body."""

    def __init__(self, event: Any) -> None:
        super().__init__("point-in-time store refused the query (leakage guard)")
        self.event = event


def decode_rows(content_type: str, content: bytes, payload: Any | None) -> list[dict[str, Any]]:
    """Decode an I2 answer: Arrow IPC stream, a JSON list of rows, ``{"rows": [...]}`` or columnar JSON."""
    if content_type.split(";")[0].strip() == ARROW_STREAM:
        table = pa.ipc.open_stream(content).read_all()
        rows: list[dict[str, Any]] = table.to_pylist()
        return rows
    if isinstance(payload, list):
        return [dict(row) for row in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("rows"), list):
            return [dict(row) for row in payload["rows"]]
        if isinstance(payload.get("columns"), list) and isinstance(payload.get("data"), list):
            columns = [str(c) for c in payload["columns"]]
            return [dict(zip(columns, values, strict=True)) for values in payload["data"]]
    raise ValueError("unrecognised point-in-time store response shape")


class PitClient(ServiceClient):
    service = "pit"

    async def query(self, query: PitQuery) -> PitResult:
        response = await self.request(
            "POST",
            "/v1/query",
            json=query.model_dump(mode="json", exclude_none=True),
            params={"format": "json"},
            ok_statuses=(HTTP_CONFLICT,),
        )
        if response.status_code == HTTP_CONFLICT:
            raise PitLeakage(response.json())
        content_type = response.headers.get("content-type", "")
        payload = None if content_type.startswith(ARROW_STREAM) else response.json()
        return PitResult(
            rows=decode_rows(content_type, response.content, payload),
            fingerprint=response.headers.get(FINGERPRINT_HEADER),
        )
