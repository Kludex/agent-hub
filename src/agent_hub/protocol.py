from __future__ import annotations

import json
import re
from typing import Any

from typing_extensions import TypedDict

JSONRPC_VERSION = "2.0"
_CAMEL_BOUNDARY = re.compile(r"_([a-z])")


class RPCRequest(TypedDict):
    jsonrpc: str
    id: str | int | None
    method: str
    params: dict[str, Any]


class RPCError(Exception):
    """A JSON-RPC error with a protocol code and optional structured data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def decode_records(body: bytes, max_record_bytes: int) -> list[RPCRequest]:
    if not body:
        raise RPCError(-32600, "Request body is empty")
    records: list[RPCRequest] = []
    for raw in body.split(b"\n"):
        if not raw:
            continue
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if len(raw) > max_record_bytes:
            raise RPCError(-32600, "JSONL record exceeds the configured limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RPCError(-32700, "Parse error") from exc
        records.append(validate_request(value))
    if not records:
        raise RPCError(-32600, "Request body contains no records")
    return records


def validate_request(value: Any) -> RPCRequest:
    if not isinstance(value, dict):
        raise RPCError(-32600, "Request must be an object")
    if value.get("jsonrpc") != JSONRPC_VERSION or not isinstance(value.get("method"), str):
        raise RPCError(-32600, "Invalid JSON-RPC request")
    request_id = value.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise RPCError(-32600, "Request id must be a string, integer, or null")
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise RPCError(-32602, "Params must be an object")
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": value["method"], "params": params}


def success(request_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": to_wire(result)}


def failure(request_id: str | int | None, error: RPCError) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        payload["data"] = to_wire(error.data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": payload}


def notification(event: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "method": "agent.event", "params": to_wire(event)}


def encode_record(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def to_wire(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _CAMEL_BOUNDARY.sub(lambda match: match.group(1).upper(), str(key)): to_wire(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_wire(item) for item in value]
    return value
