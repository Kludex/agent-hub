from __future__ import annotations

from pydantic import TypeAdapter

type JSONValue = str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]

JSON_VALUE_ADAPTER: TypeAdapter[JSONValue] = TypeAdapter(JSONValue)


def parse_json(value: str | bytes) -> JSONValue:
    return JSON_VALUE_ADAPTER.validate_json(value)
