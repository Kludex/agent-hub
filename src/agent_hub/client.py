from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx2
from pydantic import ValidationError

from agent_hub.json_data import JSONValue, parse_json


class HubClientError(RuntimeError):
    """An error returned by the Agent Hub daemon."""


class HubClient:
    def __init__(self, socket_path: Path, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.socket_path = socket_path
        self.transport = transport
        self._client: httpx2.AsyncClient | None = None

    async def __aenter__(self) -> HubClient:
        transport = self.transport or httpx2.AsyncHTTPTransport(uds=str(self.socket_path))
        self._client = httpx2.AsyncClient(transport=transport, base_url="http://agent-hub", timeout=None)
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> None:
        response = await self._request("GET", "/health")
        if response.json() != {"status": "ok"}:
            raise HubClientError("Agent Hub returned an invalid health response")

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, JSONValue]:
        request_id = uuid.uuid4().hex
        body = (
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        response = await self._request(
            "POST",
            "/v1/rpc",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        records: list[dict[str, JSONValue]] = []
        try:
            for line in response.content.splitlines():
                if line:
                    value = parse_json(line)
                    if isinstance(value, dict):
                        records.append(value)
        except ValidationError as exc:
            raise HubClientError("Agent Hub returned an invalid JSON-RPC response") from exc
        record = next((value for value in records if value.get("id") == request_id), None)
        if record is None:
            raise HubClientError("Agent Hub returned no matching JSON-RPC response")
        error = record.get("error")
        if isinstance(error, dict):
            message = error.get("message", "Agent Hub command failed")
            code = error.get("code", "unknown")
            raise HubClientError(f"{message} ({code})")
        result = record.get("result")
        if not isinstance(result, dict):
            raise HubClientError("Agent Hub returned an invalid JSON-RPC result")
        return result

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        if self._client is None:
            raise RuntimeError("Agent Hub client is not open")
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise HubClientError(f"Agent Hub is unavailable at {self.socket_path}: {exc}") from exc
        return response
