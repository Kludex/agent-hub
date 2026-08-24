from __future__ import annotations

import os

from agent_hub.json_data import JSONValue

_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def redact_text(value: str) -> str:
    redacted = value
    for key, secret in os.environ.items():
        if secret and len(secret) >= 4 and any(marker in key.upper() for marker in _SECRET_MARKERS):
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_data(value: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return {key: _redact_value(item) for key, item in value.items()}


def _redact_value(value: JSONValue) -> JSONValue:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
