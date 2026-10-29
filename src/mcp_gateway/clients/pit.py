"""Point-in-time Store (I2) client, contract 6.3, aligned with I2's shipped API.

The gateway always asks for JSON (``format`` in the request body). I2 then answers
``{schema_version, fingerprint, columns, rows, flags}`` with timestamps as ISO-8601 strings, the
``X-Dataset-Fingerprint`` header, and ``X-Leakage-Flag`` when its ``flag`` policy clamped the query. In research
mode a leaking query gets HTTP 409 with a ``LeakageEvent`` body, which I2 also reports to S6 itself.
"""

from __future__ import annotations

from typing import Any

from mcp_gateway.clients.base import ServiceClient
from mcp_gateway.contracts import PitQuery, PitResponse, PitResult, PitTrainingSetRequest

FINGERPRINT_HEADER = "X-Dataset-Fingerprint"
FLAG_HEADER = "X-Leakage-Flag"
HTTP_CONFLICT = 409


class PitLeakage(Exception):
    """I2 refused the query with 409 and a LeakageEvent body."""

    def __init__(self, event: Any) -> None:
        super().__init__("point-in-time store refused the query (leakage guard)")
        self.event = event


def _flags(header: str | None) -> list[str]:
    return [kind.strip() for kind in (header or "").split(",") if kind.strip()]


class PitClient(ServiceClient):
    service = "pit"

    async def _post(self, path: str, body: dict[str, Any]) -> PitResult:
        response = await self.request("POST", path, json=body, ok_statuses=(HTTP_CONFLICT,))
        if response.status_code == HTTP_CONFLICT:
            raise PitLeakage(response.json())
        payload = PitResponse.model_validate(response.json())
        return PitResult(
            rows=payload.rows,
            fingerprint=response.headers.get(FINGERPRINT_HEADER, payload.fingerprint),
            leakage_flags=_flags(response.headers.get(FLAG_HEADER)),
        )

    async def query(self, query: PitQuery) -> PitResult:
        return await self._post("/v1/query", query.model_dump(mode="json", exclude_none=True))

    async def training_set(self, request: PitTrainingSetRequest) -> PitResult:
        return await self._post("/v1/features/training-set", request.model_dump(mode="json", exclude_none=True))

    async def slice(self, known_at: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """Contract 6.7 market slice as known at ``known_at`` (I2 ``GET /v1/slice``)."""
        params: dict[str, Any] = {"known_at": known_at}
        if symbols:
            params["symbols"] = ",".join(symbols)
        response = await self.request("GET", "/v1/slice", params=params)
        data: dict[str, Any] = response.json()
        return data
