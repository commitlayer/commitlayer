"""A minimal MCP server over stdio for the recorder tests.

Answers ``initialize``, ``tools/list`` (two tools) and ``tools/call`` (echoes the arguments; ``isError`` is
true when ``arguments.fail`` is set). Every raw line received and every line sent is appended to the file
named by ``FAKE_MCP_LOG`` so the tests can check that traffic passed through byte for byte.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

TOOLS = [
    {
        "name": "refund_payment",
        "description": "Refund an order",
        "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
    },
    {
        "name": "lookup_order",
        "description": "Read an order",
        "inputSchema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
    },
]


def respond(message: dict[str, Any]) -> dict[str, Any]:
    method = message.get("method")
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-server", "version": "0.0.1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        arguments = (message.get("params") or {}).get("arguments") or {}
        result = {
            "content": [{"type": "text", "text": json.dumps(arguments, sort_keys=True)}],
            "isError": bool(arguments.get("fail")),
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "method not found"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> int:
    log_path = os.environ.get("FAKE_MCP_LOG")
    log = open(log_path, "ab") if log_path else None  # noqa: SIM115 - kept open for the server's lifetime
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stderr.write("fake-server: ready\n")
    sys.stderr.flush()
    for raw in iter(stdin.readline, b""):
        if log:
            log.write(b">> " + raw)
            log.flush()
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # a notification, or something that is not a request
        line = json.dumps(respond(message), separators=(",", ":")).encode() + b"\n"
        if log:
            log.write(b"<< " + line)
            log.flush()
        stdout.write(line)
        stdout.flush()
    return int(os.environ.get("FAKE_MCP_EXIT_CODE", "0"))


if __name__ == "__main__":
    sys.exit(main())
