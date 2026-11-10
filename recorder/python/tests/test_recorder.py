"""Tests for the CommitLayer MCP Recorder: the proxy end to end against a fake MCP server and a fake API,
plus unit tests for mapping validation and action bodies."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
FAKE_SERVER = HERE / "fake_server.py"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from commitlayer_mcp_recorder import (  # noqa: E402 - imported after the path is set
    LOG_PREFIX,
    MappingError,
    Recorder,
    UnresolvedPath,
    build_action,
    load_mapping,
)

TEST_KEY = "local-test-key"
REQUESTED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

MAPPING: dict[str, Any] = {
    "agent_id": "support-bot",
    "workflow_id": "refunds",
    "tools": {
        "refund_payment": {
            "contract_id": "refund_workflow",
            "action_type": "refund_order",
            "subject": "arguments.order_id",
            "subject_kind": "order",
            "params": {"amount": "arguments.amount", "currency": "arguments.currency"},
            "action_id": "refund:{arguments.order_id}",
            "compensates": "arguments.original_action_id",
        }
    },
}


# ---------------- fake API ----------------


@dataclass
class Captured:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[Captured] = []
        self.status = 201
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with api._lock:
                    api.requests.append(
                        Captured(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                    )
                if api.status >= 400:
                    payload = {"error": {"code": "server_error", "message": "simulated failure"}}
                else:
                    payload = {
                        "action": {"action_id": body.get("action_id"), "status": "pending"},
                        "verification": {"summary": {"headline": "pending"}},
                    }
                data = json.dumps(payload).encode()
                self.send_response(api.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeApi:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def api() -> Any:
    fake = FakeApi().start()
    yield fake
    fake.stop()


# ---------------- running the proxy ----------------


def request(request_id: Any, method: str, params: dict[str, Any] | None = None) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode() + b"\n"


def call(request_id: Any, name: str, arguments: dict[str, Any]) -> bytes:
    return request(request_id, "tools/call", {"name": name, "arguments": arguments})


@dataclass
class Run:
    stdout: bytes
    stderr: str
    returncode: int
    server_log: bytes = b""

    @property
    def responses(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stdout.splitlines() if line.strip()]

    def response(self, request_id: Any) -> dict[str, Any]:
        matches = [r for r in self.responses if r.get("id") == request_id]
        assert len(matches) == 1, f"expected one response for id {request_id!r}, got {matches}"
        return matches[0]

    @property
    def received_by_server(self) -> list[bytes]:
        return [line[3:] for line in self.server_log.splitlines(keepends=True) if line.startswith(b">> ")]

    @property
    def sent_by_server(self) -> list[bytes]:
        return [line[3:] for line in self.server_log.splitlines(keepends=True) if line.startswith(b"<< ")]

    @property
    def diagnostics(self) -> list[str]:
        return [line[len(LOG_PREFIX) :] for line in self.stderr.splitlines() if line.startswith(LOG_PREFIX)]


def run_recorder(
    tmp_path: Path,
    lines: list[bytes],
    *,
    api_url: str | None = None,
    mapping: dict[str, Any] | None = None,
    args: list[str] | None = None,
    api_key: str | None = TEST_KEY,
    command: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
) -> Run:
    map_path = tmp_path / "mapping.json"
    map_path.write_text(json.dumps(mapping or MAPPING))
    server_log = tmp_path / "server.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "COMMITLAYER_API_KEY")
    }
    env.update(
        {
            "PYTHONPATH": str(PACKAGE_ROOT),
            "NO_PROXY": "127.0.0.1,localhost",
            "FAKE_MCP_LOG": str(server_log),
            "COMMITLAYER_API_URL": api_url or "http://127.0.0.1:9",
        }
    )
    if api_key is not None:
        env["COMMITLAYER_API_KEY"] = api_key
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "commitlayer_mcp_recorder",
            "--map",
            str(map_path),
            *(args or []),
            "--",
            *(command or [sys.executable, str(FAKE_SERVER)]),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = proc.communicate(b"".join(lines), timeout=60)
    log_bytes = server_log.read_bytes() if server_log.exists() else b""
    return Run(out, err.decode(errors="replace"), proc.returncode, log_bytes)


# ---------------- proxy behaviour ----------------


def test_initialize_and_tools_list_pass_through_byte_for_byte(tmp_path: Path, api: FakeApi) -> None:
    lines = [
        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
        request("list-1", "tools/list", {}),
    ]
    run = run_recorder(tmp_path, lines, api_url=api.url)
    assert run.returncode == 0
    assert run.received_by_server == lines
    assert run.stdout.splitlines(keepends=True) == run.sent_by_server
    assert [r["id"] for r in run.responses] == [1, "list-1"]
    assert [t["name"] for t in run.response("list-1")["result"]["tools"]] == [
        "refund_payment",
        "lookup_order",
    ]
    assert api.requests == []
    assert "fake-server: ready" in run.stderr


def test_mapped_call_is_recorded_before_forwarding(tmp_path: Path, api: FakeApi) -> None:
    line = call(7, "refund_payment", {"order_id": "order_10231", "amount": 180.0, "currency": "USD"})
    run = run_recorder(tmp_path, [line], api_url=api.url)

    assert len(api.requests) == 1
    posted = api.requests[0]
    assert posted.path == "/v1/actions"
    assert posted.headers["x-api-key"] == TEST_KEY
    assert posted.headers["content-type"] == "application/json"
    body = posted.body
    assert body["action_id"] == "refund:order_10231"
    assert body["contract_id"] == "refund_workflow"
    assert body["subject_id"] == "order_10231"
    assert body["subject_kind"] == "order"
    assert body["action_type"] == "refund_order"
    assert body["agent_id"] == "support-bot"
    assert body["workflow_id"] == "refunds"
    assert body["channel"] == "mcp"
    assert REQUESTED_AT.match(body["requested_at"])
    assert body["params"] == {
        "amount": 180.0,
        "currency": "USD",
        "mcp_tool": "refund_payment",
        "mcp_request_id": "7",
    }
    assert "compensates" not in body

    assert run.received_by_server == [line]
    result = run.response(7)["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {
        "order_id": "order_10231",
        "amount": 180.0,
        "currency": "USD",
    }
    assert "recorded refund:order_10231 → tool result isError=false" in run.diagnostics
    assert TEST_KEY not in run.stderr


def test_tool_error_is_logged_but_not_sent(tmp_path: Path, api: FakeApi) -> None:
    run = run_recorder(
        tmp_path, [call(8, "refund_payment", {"order_id": "o1", "fail": True})], api_url=api.url
    )
    assert len(api.requests) == 1
    assert run.response(8)["result"]["isError"] is True
    assert "recorded refund:o1 → tool result isError=true" in run.diagnostics


def test_cli_flags_override_mapping_ids(tmp_path: Path, api: FakeApi) -> None:
    run_recorder(
        tmp_path,
        [call(1, "refund_payment", {"order_id": "o1"})],
        api_url=api.url,
        args=["--agent-id", "other-bot", "--workflow-id", "other-flow"],
    )
    assert api.requests[0].body["agent_id"] == "other-bot"
    assert api.requests[0].body["workflow_id"] == "other-flow"


def test_unmapped_call_is_forwarded_without_recording(tmp_path: Path, api: FakeApi) -> None:
    lines = [call(1, "lookup_order", {"order_id": "o1"}), call(2, "lookup_order", {"order_id": "o2"})]
    run = run_recorder(tmp_path, lines, api_url=api.url, args=["--warn-unmapped"])
    assert api.requests == []
    assert run.received_by_server == lines
    assert run.response(1)["result"]["isError"] is False
    assert run.response(2)["result"]["isError"] is False
    assert run.diagnostics.count("unmapped tool 'lookup_order' forwarded without recording") == 1


def test_api_failure_is_fail_open_by_default(tmp_path: Path, api: FakeApi) -> None:
    api.status = 500
    line = call(3, "refund_payment", {"order_id": "o3"})
    run = run_recorder(tmp_path, [line], api_url=api.url)
    assert len(api.requests) == 1
    assert run.received_by_server == [line]
    assert run.response(3)["result"]["isError"] is False
    assert any(
        d.startswith("action refund:o3 not recorded (HTTP 500") and "forwarding anyway" in d
        for d in run.diagnostics
    )


def test_unreachable_api_is_fail_open_by_default(tmp_path: Path) -> None:
    line = call(4, "refund_payment", {"order_id": "o4"})
    run = run_recorder(tmp_path, [line], api_url="http://127.0.0.1:9", args=["--timeout", "2"])
    assert run.received_by_server == [line]
    assert run.response(4)["result"]["isError"] is False
    assert any("not recorded (cannot reach" in d for d in run.diagnostics)


def test_no_fail_open_refuses_the_call_with_a_jsonrpc_error(tmp_path: Path, api: FakeApi) -> None:
    api.status = 500
    lines = [
        request(1, "initialize", {}),
        call(5, "refund_payment", {"order_id": "o5"}),
        call(6, "lookup_order", {}),
    ]
    run = run_recorder(tmp_path, lines, api_url=api.url, args=["--no-fail-open"])
    assert len(api.requests) == 1
    assert run.received_by_server == [lines[0], lines[2]]
    error = run.response(5)
    assert error["jsonrpc"] == "2.0"
    assert "result" not in error
    assert error["error"]["code"] == -32000
    assert error["error"]["message"].startswith("CommitLayer recording failed: HTTP 500")
    assert "result" in run.response(1)
    assert "result" in run.response(6)


def test_no_api_key_is_a_passthrough_with_one_warning(tmp_path: Path, api: FakeApi) -> None:
    lines = [call(1, "refund_payment", {"order_id": "o1"}), call(2, "refund_payment", {"order_id": "o2"})]
    run = run_recorder(tmp_path, lines, api_url=api.url, api_key=None)
    assert api.requests == []
    assert run.received_by_server == lines
    assert "result" in run.response(1) and "result" in run.response(2)
    assert (
        run.diagnostics.count(
            "COMMITLAYER_API_KEY is not set; running as a passthrough, nothing will be recorded"
        )
        == 1
    )
    assert run.returncode == 0


def test_compensates_path_is_carried(tmp_path: Path, api: FakeApi) -> None:
    run_recorder(
        tmp_path,
        [call(9, "refund_payment", {"order_id": "o9", "original_action_id": "refund:o9:first"})],
        api_url=api.url,
    )
    assert api.requests[0].body["compensates"] == "refund:o9:first"
    assert api.requests[0].body["action_id"] == "refund:o9"


def test_missing_subject_is_forwarded_without_recording(tmp_path: Path, api: FakeApi) -> None:
    line = call(10, "refund_payment", {"amount": 5})
    run = run_recorder(tmp_path, [line], api_url=api.url)
    assert api.requests == []
    assert run.received_by_server == [line]
    assert run.response(10)["result"]["isError"] is False
    assert (
        "tool 'refund_payment' forwarded without recording: path arguments.order_id not found in the call"
        in run.diagnostics
    )


def test_prefixed_mapping_key_matches_bare_tool_name(tmp_path: Path, api: FakeApi) -> None:
    mapping = {
        "tools": {
            "stripe.refund_payment": {"contract_id": "refund_workflow", "subject": "arguments.order_id"}
        }
    }
    run = run_recorder(
        tmp_path, [call(11, "refund_payment", {"order_id": "o11"})], api_url=api.url, mapping=mapping
    )
    assert len(api.requests) == 1
    body = api.requests[0].body
    assert body["action_id"] == "mcp:refund_payment:11"
    assert body["params"] == {"mcp_tool": "refund_payment", "mcp_request_id": "11"}
    assert "agent_id" not in body and "workflow_id" not in body
    assert "result" in run.response(11)


def test_non_json_lines_are_forwarded_untouched(tmp_path: Path, api: FakeApi) -> None:
    lines = [b"this is not json\n", request(1, "tools/list", {})]
    run = run_recorder(tmp_path, lines, api_url=api.url)
    assert run.received_by_server == lines
    assert "result" in run.response(1)


def test_downstream_exit_code_is_propagated(tmp_path: Path, api: FakeApi) -> None:
    run = run_recorder(
        tmp_path, [request(1, "tools/list", {})], api_url=api.url, env_extra={"FAKE_MCP_EXIT_CODE": "3"}
    )
    assert run.returncode == 3
    assert "result" in run.response(1)


def test_invalid_mapping_file_exits_with_an_error(tmp_path: Path) -> None:
    run = run_recorder(tmp_path, [], mapping={"tools": {"t": {"subject": "arguments.x"}}})
    assert run.returncode == 2
    assert any(d.startswith("invalid mapping") and "contract_id" in d for d in run.diagnostics)


# ---------------- load_mapping ----------------


def test_load_mapping_from_file_and_dict(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps(MAPPING))
    from_file = load_mapping(path)
    from_dict = load_mapping(MAPPING)
    assert from_file == from_dict
    assert from_file.agent_id == "support-bot"
    assert from_file.workflow_id == "refunds"
    tool = from_file.tools["refund_payment"]
    assert tool.contract_id == "refund_workflow"
    assert tool.params == {"amount": "arguments.amount", "currency": "arguments.currency"}
    assert tool.action_id == "refund:{arguments.order_id}"
    assert tool.compensates == "arguments.original_action_id"
    assert from_file.lookup("refund_payment") is tool
    assert from_file.lookup("lookup_order") is None


def test_load_mapping_example_file() -> None:
    mapping = load_mapping(PACKAGE_ROOT / "examples" / "mapping.json")
    assert set(mapping.tools) == {"refund_payment", "zendesk.update_ticket"}
    assert mapping.lookup("update_ticket") is mapping.tools["zendesk.update_ticket"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({}, "non-empty 'tools'"),
        ({"tools": {}}, "non-empty 'tools'"),
        ({"tools": {"t": "x"}}, "tools.t must be an object"),
        ({"tools": {"t": {"subject": "arguments.x"}}}, "tools.t is missing required key 'contract_id'"),
        ({"tools": {"t": {"contract_id": "c"}}}, "tools.t is missing required key 'subject'"),
        (
            {"tools": {"t": {"contract_id": "", "subject": "s"}}},
            "tools.t.contract_id must be a non-empty string",
        ),
        (
            {"tools": {"t": {"contract_id": "c", "subject": "s", "params": ["a"]}}},
            "tools.t.params must be an object",
        ),
        (
            {"tools": {"t": {"contract_id": "c", "subject": "s", "params": {"a": 1}}}},
            "tools.t.params.a must be",
        ),
        (
            {"tools": {"t": {"contract_id": "c", "subject": "s", "contract_version": "1"}}},
            "contract_version must be an integer",
        ),
        ({"tools": {"t": {"contract_id": "c", "subject": "s", "action_id": 5}}}, "tools.t.action_id must be"),
        (
            {"agent_id": 3, "tools": {"t": {"contract_id": "c", "subject": "s"}}},
            "agent_id must be a non-empty string",
        ),
    ],
)
def test_load_mapping_rejects_invalid_shapes(raw: Any, message: str) -> None:
    with pytest.raises(MappingError, match=re.escape(message)):
        load_mapping(raw)


def test_load_mapping_allows_unknown_keys() -> None:
    mapping = load_mapping(
        {"notes": "x", "tools": {"t": {"contract_id": "c", "subject": "s", "extra": True}}}
    )
    assert mapping.tools["t"].contract_id == "c"


def test_load_mapping_reports_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(MappingError, match="cannot read mapping file"):
        load_mapping(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(MappingError, match="not valid JSON"):
        load_mapping(bad)
    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[]")
    with pytest.raises(MappingError, match="mapping must be an object"):
        load_mapping(not_an_object)


def test_load_mapping_yaml(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(MAPPING))
    assert load_mapping(path) == load_mapping(MAPPING)


# ---------------- build_action ----------------


def test_build_action_resolves_paths_and_templates() -> None:
    tool = load_mapping(MAPPING).tools["refund_payment"]
    body = build_action(
        "refund_payment",
        {"name": "refund_payment", "arguments": {"order_id": "o1", "amount": 10, "original_action_id": "a0"}},
        tool,
        request_id=42,
        agent_id="bot",
        workflow_id="wf",
        requested_at="2026-10-15T12:00:00Z",
    )
    assert body == {
        "action_id": "refund:o1",
        "contract_id": "refund_workflow",
        "subject_id": "o1",
        "requested_at": "2026-10-15T12:00:00Z",
        "params": {"amount": 10, "mcp_tool": "refund_payment", "mcp_request_id": "42"},
        "channel": "mcp",
        "workflow_id": "wf",
        "agent_id": "bot",
        "action_type": "refund_order",
        "subject_kind": "order",
        "compensates": "a0",
    }


def test_build_action_defaults_and_requested_at_format() -> None:
    mapping = load_mapping({"tools": {"t": {"contract_id": "c", "subject": "name", "contract_version": 3}}})
    body = build_action("t", {"name": "t", "arguments": {}}, mapping.tools["t"], request_id="req-1")
    assert body["action_id"] == "mcp:t:req-1"
    assert body["subject_id"] == "t"
    assert body["contract_version"] == 3
    assert body["params"] == {"mcp_tool": "t", "mcp_request_id": "req-1"}
    assert REQUESTED_AT.match(body["requested_at"])
    assert "compensates" not in body and "agent_id" not in body


def test_build_action_paths_into_meta_and_lists() -> None:
    mapping = load_mapping(
        {
            "tools": {
                "t": {
                    "contract_id": "c",
                    "subject": "_meta.progressToken",
                    "params": {"first": "arguments.items.0"},
                }
            }
        }
    )
    body = build_action(
        "t",
        {"name": "t", "_meta": {"progressToken": 9}, "arguments": {"items": ["a"]}},
        mapping.tools["t"],
        request_id=1,
    )
    assert body["subject_id"] == "9"
    assert body["params"]["first"] == "a"


def test_build_action_missing_subject_or_template_field_raises() -> None:
    tool = load_mapping(MAPPING).tools["refund_payment"]
    with pytest.raises(UnresolvedPath) as info:
        build_action("refund_payment", {"name": "refund_payment", "arguments": {}}, tool, request_id=1)
    assert str(info.value) == "arguments.order_id"
    with pytest.raises(UnresolvedPath):
        build_action(
            "refund_payment", {"name": "refund_payment", "arguments": {"order_id": None}}, tool, request_id=1
        )
    templated = load_mapping(
        {"tools": {"t": {"contract_id": "c", "subject": "name", "action_id": "x:{arguments.k}"}}}
    )
    with pytest.raises(UnresolvedPath, match="arguments.k"):
        build_action("t", {"name": "t", "arguments": {}}, templated.tools["t"], request_id=1)


# ---------------- Recorder (in process, with a fake transport) ----------------


def test_recorder_matches_responses_by_exact_id() -> None:
    posted: list[dict[str, Any]] = []
    logged: list[str] = []
    recorder = Recorder(
        load_mapping(MAPPING), transport=lambda body: posted.append(body) or {}, log=logged.append
    )
    decision = recorder.before_forward(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "refund_payment", "arguments": {"order_id": "o1"}},
        }
    )
    assert decision.forward and decision.action_id == "refund:o1"
    assert len(posted) == 1
    assert recorder.after_response({"jsonrpc": "2.0", "id": "1", "result": {}}) is None  # "1" is not 1
    assert (
        recorder.after_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}})
        == "refund:o1"
    )
    assert "recorded refund:o1 → tool result isError=true" in logged
    assert recorder.pending() == {}
