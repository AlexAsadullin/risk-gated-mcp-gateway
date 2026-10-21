"""Shared plumbing for downstream service clients."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from mcp_gateway.settings import ServiceEndpoint

log = structlog.get_logger(__name__)

SERVER_ERROR_MIN = 500


class DownstreamError(Exception):
    """A downstream service answered, but not with success."""

    def __init__(self, service: str, status_code: int, body: Any) -> None:
        super().__init__(f"{service} returned HTTP {status_code}")
        self.service = service
        self.status_code = status_code
        self.body = body


class DownstreamUnavailable(Exception):
    """A downstream service could not be reached (connection error, timeout or 5xx)."""

    def __init__(self, service: str, reason: str) -> None:
        super().__init__(f"{service} unavailable: {reason}")
        self.service = service
        self.reason = reason


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


class ServiceClient:
    """Async JSON client bound to one service base URL. Pass ``transport`` to run against in-process fakes."""

    service: str = "service"

    def __init__(self, endpoint: ServiceEndpoint, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=endpoint.base_url, timeout=endpoint.timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        """Send a request; raise :class:`DownstreamUnavailable` or :class:`DownstreamError` on failure.

        Statuses in ``ok_statuses`` are returned to the caller instead of raising (e.g. I2's 409).
        """
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            log.warning("downstream_unreachable", service=self.service, path=path, error=str(exc))
            raise DownstreamUnavailable(self.service, type(exc).__name__) from exc
        if response.status_code in ok_statuses or response.is_success:
            return response
        if response.status_code >= SERVER_ERROR_MIN:
            raise DownstreamUnavailable(self.service, f"HTTP {response.status_code}")
        raise DownstreamError(self.service, response.status_code, _body(response))
