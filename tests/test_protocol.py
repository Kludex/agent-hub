from __future__ import annotations

import pytest

from agent_hub.protocol import RPCError, decode_records, failure, notification, success, validate_request


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"\n\n", -32600),
        (b"{}\n", -32600),
        (b'{"jsonrpc":"2.0","method":"x","id":[]}\n', -32600),
        (b'{"jsonrpc":"2.0","method":"x","params":[]}\n', -32602),
        (b"\xff\n", -32700),
    ],
)
def test_rejects_invalid_jsonl_requests(body: bytes, code: int) -> None:
    with pytest.raises(RPCError) as captured:
        decode_records(body, 1024)

    assert captured.value.code == code


def test_enforces_record_size_and_accepts_crlf() -> None:
    with pytest.raises(RPCError, match="exceeds"):
        decode_records(b'{"jsonrpc":"2.0","method":"snapshot"}\n', 4)

    records = decode_records(b'{"jsonrpc":"2.0","id":null,"method":"snapshot"}\r\n', 1024)

    assert records[0]["method"] == "snapshot"


def test_protocol_helpers_convert_nested_values_to_wire_casing() -> None:
    assert success(1, {"agent_id": "a", "items": ({"run_id": "r"},)}) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"agentId": "a", "items": [{"runId": "r"}]},
    }
    assert failure(None, RPCError(1, "bad", {"agent_id": "a"}))["error"]["data"] == {"agentId": "a"}
    assert notification({"run_id": "r"})["params"] == {"runId": "r"}
    assert validate_request({"jsonrpc": "2.0", "method": "x"})["params"] == {}
