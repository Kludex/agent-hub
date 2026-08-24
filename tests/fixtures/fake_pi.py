#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

write_lock = threading.Lock()
session_directory = Path(sys.argv[sys.argv.index("--session-dir") + 1])
session_directory.mkdir(parents=True, exist_ok=True)
session_file = session_directory / "fake-session.jsonl"
session_file.touch()
system_prompt = sys.argv[sys.argv.index("--append-system-prompt") + 1] if "--append-system-prompt" in sys.argv else None
last_text: str | None = None
settled = threading.Event()


def emit(value: dict[str, Any], *, split: bool = False) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    with write_lock:
        if split:
            midpoint = max(1, len(data) // 2)
            os.write(sys.stdout.fileno(), data[:midpoint])
            os.write(sys.stdout.fileno(), data[midpoint:])
        else:
            os.write(sys.stdout.fileno(), data)


def response(command: dict[str, Any], data: dict[str, Any] | None = None) -> None:
    emit({"id": command.get("id"), "type": "response", "command": command["type"], "success": True, "data": data})


def complete_prompt(message: str) -> None:
    global last_text
    emit({"type": "agent_start"})
    if message == "thinking":
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "thought"},
            }
        )
    emit(
        {
            "type": "message_update",
            "usage": {"input": 4, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 6},
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": f"reply :{message}"},
        },
        split=True,
    )
    emit({"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "read", "args": {"path": "x"}})
    emit(
        {
            "type": "tool_execution_update",
            "toolCallId": "call_1",
            "toolName": "read",
            "args": {"path": "x"},
            "partialResult": {"content": [{"type": "text", "text": "partial"}]},
        }
    )
    emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "call_1",
            "toolName": "read",
            "result": {"content": [{"type": "text", "text": "done"}]},
            "isError": False,
        }
    )
    os.write(sys.stderr.fileno(), b"fixture stderr\n")
    if message == "stderr-overflow":
        time.sleep(0.01)
        os.write(sys.stderr.fileno(), b"additional stderr\n")
    last_text = f"result:{message}"
    if message == "profile-instructions":
        last_text = f"{last_text}:{system_prompt}"
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    time.sleep(0.01)
    emit({"type": "agent_settled"})
    settled.set()


for raw in sys.stdin.buffer:
    command = json.loads(raw)
    command_type = command["type"]
    if command_type == "prompt":
        message = command["message"]
        if message not in {"no-response", "oversized-no-newline"}:
            response(command)
        settled.clear()
        if message == "malformed":
            os.write(sys.stdout.fileno(), b"{not json}\n")
        elif message == "non-object":
            os.write(sys.stdout.fileno(), b"[]\n")
        elif message == "oversized":
            os.write(sys.stdout.fileno(), b"{" + (b"x" * 2048) + b"}\n")
        elif message in {"oversized-no-newline", "oversized-combined"}:
            os.write(sys.stdout.fileno(), b"{" + (b"x" * 2048))
        elif message == "no-response":
            os.close(sys.stdout.fileno())
        elif message == "incomplete":
            os.write(sys.stdout.fileno(), b"{")
            os.close(sys.stdout.fileno())
        elif message == "crash":
            os._exit(7)
        elif message in {"wait", "stubborn"}:
            if message == "stubborn":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)

            def delayed_prompt() -> None:
                time.sleep(30)
                complete_prompt(message)

            threading.Thread(target=delayed_prompt, daemon=True).start()
        elif message == "crlf":
            os.write(sys.stdout.fileno(), b'{"type":"agent_start"}\r\n')
            complete_prompt(message)
        elif message == "retry":
            emit({"type": "agent_start"})
            emit({"type": "agent_end", "messages": [], "willRetry": True})
            complete_prompt(message)
        else:
            complete_prompt(message)
    elif command_type in {"steer", "follow_up"}:
        if command.get("message") == "reject":
            emit(
                {
                    "id": command.get("id"),
                    "type": "response",
                    "command": command_type,
                    "success": False,
                    "error": "rejected",
                }
            )
        else:
            response(command)
    elif command_type == "abort":
        response(command)
        emit({"type": "agent_settled"})
        settled.set()
    elif command_type == "get_last_assistant_text":
        response(command, {"text": last_text})
    elif command_type == "get_session_stats":
        response(
            command,
            {
                "tokens": {"input": 4, "output": 2, "cacheRead": 0, "cacheWrite": 0, "total": 6},
                "cost": 0.02,
            },
        )
    elif command_type == "get_state":
        response(command, {"sessionFile": str(session_file), "sessionId": "fake-session", "isStreaming": False})
    elif command_type == "switch_session":
        response(command, {"cancelled": "cancel" in command["sessionPath"]})
    else:
        emit(
            {
                "id": command.get("id"),
                "type": "response",
                "command": command_type,
                "success": False,
                "error": f"unsupported command: {command_type}",
            }
        )
