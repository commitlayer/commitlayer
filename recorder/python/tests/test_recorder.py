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

import commitlayer_mcp_recorder  # noqa: E402 - imported after the path is set
from commitlayer_mcp_recorder import (  # noqa: E402
    LOG_PREFIX,
    MappingError,
    Recorder,
    RecordingError,
    UnresolvedPath,
    __version__,
    build_action,
    load_mapping,
    post_action,
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
    raw: bytes = b""


class FakeApi:
    """Answers ``status`` to every request, after the answers queued in ``script`` (a status and the extra
    response headers, such as ``Retry-After``) have been used up, one per request."""

    def __init__(self) -> None:
        self.requests: list[Captured] = []
        self.status = 201
        self.script: list[tuple[int, dict[str, str]]] = []
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw or b"{}")
                with api._lock:
                    api.requests.append(
                        Captured(self.path, {k.lower(): v for k, v in self.headers.items()}, body, raw)
                    )
                    status, extra = api.script.pop(0) if api.script else (api.status, {})
                if status >= 400:
                    payload = {"error": {"code": "server_error", "message": "simulated failure"}}
                else:
                    payload = {
                        "action": {"action_id": body.get("action_id"), "status": "pending"},
                        "verification": {"summary": {"headline": "pending"}},
                    }
                data = json.dumps(payload).encode()
                self.send_response(status)
                for name, value in extra.items():
                    self.send_header(name, value)
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


def test_subject_template_spells_out_the_systems_prefix(tmp_path: Path, api: FakeApi) -> None:
    mapping = {
        "tools": {
            "zendesk.update_ticket": {
                "contract_id": "scop_resolve_customer_case",
                "subject": "zendesk_ticket_{arguments.ticket_id}",
                "subject_kind": "ticket",
                "action_id": "resolve:zendesk_ticket_{arguments.ticket_id}",
            }
        }
    }
    run_recorder(tmp_path, [call(12, "update_ticket", {"ticket_id": 4411, "status": "solved"})], api_url=api.url, mapping=mapping)
    assert api.requests[0].body["subject_id"] == "zendesk_ticket_4411"
    assert api.requests[0].body["action_id"] == "resolve:zendesk_ticket_4411"
    # a template whose path is absent is treated like a missing subject: forwarded, not recorded
    run = run_recorder(tmp_path, [call(13, "update_ticket", {"status": "solved"})], api_url=api.url, mapping=mapping)
    assert len(api.requests) == 1 and run.response(13)["result"]["isError"] is False
    assert any("forwarded without recording" in d for d in run.diagnostics)


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


# ---------------- retries: 429 and 503 with Retry-After ----------------

REFUND_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "refund_payment", "arguments": {"order_id": "o1"}},
}


@pytest.fixture
def direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake API is on a loopback port and must be reached directly, whatever proxy the environment names."""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")


class FakeClock:
    """A clock that only moves when the recorder waits, so the time left for a record is exact."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def recorder_for(api: FakeApi | None, **options: Any) -> tuple[Recorder, list[float], list[str]]:
    """A recorder whose waits move a fake clock instead of sleeping and whose jitter is zero. With an API it
    posts over HTTP; without one the caller passes a ``transport``."""
    clock = FakeClock()
    logged: list[str] = []
    options.setdefault("jitter", lambda: 0.0)
    if api is not None:
        options.update(api_key=TEST_KEY, api_url=api.url)
    recorder = Recorder(load_mapping(MAPPING), sleep=clock.sleep, clock=clock, log=logged.append, **options)
    return recorder, clock.waits, logged


def refusing(*failures: RecordingError) -> Any:
    """A transport that raises the given failures in turn and then records."""
    left = list(failures)
    calls: list[dict[str, Any]] = []

    def transport(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        if left:
            raise left.pop(0)
        return {}

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def busy(status: int = 503, retry_after: float | None = 1.0) -> RecordingError:
    return RecordingError(f"HTTP {status}", status=status, retry_after=retry_after)


def test_version_constant_matches_the_package_version() -> None:
    text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'(?m)^version = "([^"]+)"$', text)
    assert declared is not None and __version__ == declared.group(1)


def test_post_action_error_carries_the_status_and_the_stated_wait(api: FakeApi, direct: None) -> None:
    api.script = [
        (429, {"Retry-After": "1"}),
        (503, {"retry-after": "2.5"}),
        (503, {}),
        (500, {"Retry-After": "1"}),
        # the header twice (a proxy added its own): the longer wait is the one stated, whichever came first
        (503, {"Retry-After": "1", "retry-after": "3600"}),
        (503, {"Retry-After": "3600", "retry-after": "1"}),
        (429, {"Retry-After": "soon", "retry-after": "2"}),
    ]
    seen = []
    for _ in api.script[:]:
        with pytest.raises(RecordingError) as e:
            post_action(api.url, TEST_KEY, {"action_id": "a"})
        seen.append((e.value.status, e.value.retry_after))
        assert str(e.value).startswith(f"HTTP {e.value.status} from {api.url}/v1/actions: ")
    assert seen == [(429, 1.0), (503, 2.5), (503, None), (500, 1.0), (503, 3600.0), (503, 3600.0), (429, 2.0)]
    with pytest.raises(RecordingError) as e:
        post_action("http://127.0.0.1:9", TEST_KEY, {"action_id": "a"}, timeout=2)
    assert (e.value.status, e.value.retry_after) == (None, None)
    plain = RecordingError("as it was raised before")
    assert (str(plain), plain.status, plain.retry_after) == ("as it was raised before", None, None)


@pytest.mark.parametrize("status", [429, 503])
def test_one_refusal_then_success_is_recorded_with_the_same_body(api: FakeApi, direct: None, status: int) -> None:
    api.script = [(status, {"Retry-After": "1"})]
    recorder, waits, logged = recorder_for(api)
    decision = recorder.before_forward(REFUND_CALL)
    assert decision.forward and decision.action_id == "refund:o1" and decision.error is None
    assert waits == [1.0]
    assert len(api.requests) == 2 and api.requests[0].raw == api.requests[1].raw  # byte for byte
    assert api.requests[0].body["action_id"] == "refund:o1"
    assert logged == [
        f"action refund:o1 refused (HTTP {status}); retry 1 of 2 in 1.00 s",
        "recorded refund:o1 for tool 'refund_payment'",
    ]
    assert recorder.pending() == {"1": "refund:o1"}
    assert TEST_KEY not in "".join(logged)


def test_default_action_id_and_request_time_are_the_same_on_the_retry(api: FakeApi, direct: None) -> None:
    api.script = [(503, {"Retry-After": "0.5"})]
    mapping = {"tools": {"lookup_order": {"contract_id": "c", "subject": "arguments.order_id"}}}
    clock = FakeClock()
    recorder = Recorder(
        load_mapping(mapping), api_key=TEST_KEY, api_url=api.url, sleep=clock.sleep, clock=clock,
        jitter=lambda: 0.0, log=lambda line: None,
    )  # fmt: skip
    message = {"jsonrpc": "2.0", "id": "req-9", "method": "tools/call", "params": {"name": "lookup_order", "arguments": {"order_id": "o9"}}}  # fmt: skip
    assert recorder.before_forward(message).action_id == "mcp:lookup_order:req-9"
    assert clock.waits == [0.5] and api.requests[0].raw == api.requests[1].raw
    assert api.requests[1].body["action_id"] == "mcp:lookup_order:req-9"


def test_refusals_past_the_limit_fall_to_the_fail_open_policy(api: FakeApi, direct: None) -> None:
    refusals = [(429, {"Retry-After": "1"}), (503, {"Retry-After": "1"}), (429, {"Retry-After": "1"}), (201, {})]
    api.script = list(refusals)
    recorder, waits, logged = recorder_for(api)  # two retries is the default
    decision = recorder.before_forward(REFUND_CALL)
    assert decision.forward and decision.action_id is None and decision.error is None
    assert waits == [1.0, 1.0] and len(api.requests) == 3 and len({r.raw for r in api.requests}) == 1
    assert logged[:2] == [
        "action refund:o1 refused (HTTP 429); retry 1 of 2 in 1.00 s",
        "action refund:o1 refused (HTTP 503); retry 2 of 2 in 1.00 s",
    ]
    assert logged[2].startswith("action refund:o1 not recorded (HTTP 429 from ")  # the last answer
    assert logged[2].endswith("(after 3 attempts)); forwarding anyway")
    assert recorder.pending() == {}

    api.requests.clear()
    api.script = list(refusals)
    recorder, waits, logged = recorder_for(api, fail_open=False, max_retries=1)
    decision = recorder.before_forward(REFUND_CALL)
    assert not decision.forward and decision.action_id == "refund:o1"
    assert decision.error is not None
    message = decision.error["error"]["message"]
    assert message.startswith("CommitLayer recording failed: HTTP 503 from ") and message.endswith("(after 2 attempts)")
    assert waits == [1.0] and len(api.requests) == 2

    api.requests.clear()
    api.script = list(refusals)
    recorder, waits, logged = recorder_for(api, max_retries=3)
    assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1"
    assert waits == [1.0, 1.0, 1.0] and len(api.requests) == 4


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (400, {"Retry-After": "1"}),
        (402, {"Retry-After": "1"}),
        (409, {"Retry-After": "1"}),
        (500, {"Retry-After": "1"}),
        (502, {"Retry-After": "1"}),
        (429, {}),
        (503, {}),
        (429, {"Retry-After": "soon"}),
        (503, {"Retry-After": "-1"}),
        (429, {"Retry-After": "9" * 400}),
        (429, {"Retry-After": "5"}),  # the whole of the record's five seconds: it cannot fit
        (429, {"Retry-After": "4"}),  # it fits, but the attempt after it would not have its second
        (503, {"Retry-After": "1", "retry-after": "41213"}),  # stated twice: the longer wait counts
        (429, {"Retry-After": "41213"}),  # the daily cap states the seconds to midnight
    ],
)
def test_nothing_else_is_retried_or_waited(api: FakeApi, direct: None, status: int, headers: dict[str, str]) -> None:
    api.script = [(status, headers)]
    recorder, waits, logged = recorder_for(api, max_retries=3)
    decision = recorder.before_forward(REFUND_CALL)
    assert decision.forward and decision.action_id is None  # fail-open: forwarded, not recorded
    assert waits == [] and len(api.requests) == 1
    assert len(logged) == 1 and logged[0].startswith(f"action refund:o1 not recorded (HTTP {status} from ")
    assert "attempts" not in logged[0]


def test_every_attempt_and_every_wait_fit_in_the_time_of_one_record() -> None:
    # six seconds for the record, the API asks for two each time: two waits fit with a second left for the
    # third attempt; a third wait would end at the deadline
    transport = refusing(*[busy(retry_after=2.0) for _ in range(4)])
    recorder, waits, logged = recorder_for(None, transport=transport, max_retries=10, timeout=6.0)
    decision = recorder.before_forward(REFUND_CALL)
    assert decision.forward and decision.action_id is None
    assert waits == [2.0, 2.0] and sum(waits) < 6.0 and len(transport.calls) == 3
    assert logged[-1] == "action refund:o1 not recorded (HTTP 503 (after 3 attempts)); forwarding anyway"

    # a wait is started only when a second of the record's time is left for the attempt after it
    transport = refusing(busy(retry_after=2.0))
    recorder, waits, logged = recorder_for(None, transport=transport, timeout=3.0)
    assert recorder.before_forward(REFUND_CALL).action_id is None and waits == [] and len(transport.calls) == 1
    transport = refusing(busy(retry_after=1.99))
    recorder, waits, logged = recorder_for(None, transport=transport, timeout=3.0)
    assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1" and waits == [1.99]


def test_a_refusal_that_arrives_late_is_not_followed_by_an_attempt_with_no_time_to_finish() -> None:
    # The API held the request 3.9 s before refusing it. A retry after the stated second would have a tenth of
    # a second: it would time out here while the API may still store the action, and the recorder would then
    # refuse a tool call whose action exists. So the record is sent once, and what the recorder says is true.
    clock = FakeClock()
    calls: list[dict[str, Any]] = []
    logged: list[str] = []

    def slow_refusal(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        if len(calls) == 1:
            clock.now += 3.9
            raise busy(503, 1.0)
        return {}

    recorder = Recorder(
        load_mapping(MAPPING), transport=slow_refusal, sleep=clock.sleep, clock=clock, jitter=lambda: 0.0,
        log=logged.append, fail_open=False,
    )  # fmt: skip
    decision = recorder.before_forward(REFUND_CALL)
    assert not decision.forward and len(calls) == 1 and clock.waits == []
    assert logged == ["action refund:o1 not recorded (HTTP 503); call refused because fail-open is off"]

    # the same refusal one second earlier leaves room for the wait and a second for the attempt
    clock.now, calls[:] = 1000.0, []
    held = [2.9]

    def refusal_in_time(body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        if held:
            clock.now += held.pop()
            raise busy(503, 1.0)
        return {}

    recorder = Recorder(
        load_mapping(MAPPING), transport=refusal_in_time, sleep=clock.sleep, clock=clock, jitter=lambda: 0.0,
        log=logged.append, fail_open=False,
    )  # fmt: skip
    assert recorder.before_forward(REFUND_CALL).forward and len(calls) == 2 and clock.waits == [1.0]


def test_a_wait_that_runs_past_the_deadline_is_not_followed_by_an_attempt() -> None:
    clock = FakeClock()
    transport = refusing(busy(503, 1.0))

    def overslept(seconds: float) -> None:  # the machine was suspended during the wait
        clock.waits.append(seconds)
        clock.now += 60.0

    logged: list[str] = []
    recorder = Recorder(
        load_mapping(MAPPING), transport=transport, sleep=overslept, clock=clock, jitter=lambda: 0.0, log=logged.append
    )
    decision = recorder.before_forward(REFUND_CALL)
    assert decision.forward and decision.action_id is None
    assert clock.waits == [1.0] and len(transport.calls) == 1
    assert logged[-1] == "action refund:o1 not recorded (HTTP 503); forwarding anyway"


def test_without_a_timeout_the_record_is_sent_once_as_it_always_was(
    api: FakeApi, direct: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # timeout=None is no limit on the request; it recorded before retries existed and still does
    recorder, waits, logged = recorder_for(api, timeout=None)
    assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1"
    assert len(api.requests) == 1 and logged == ["recorded refund:o1 for tool 'refund_payment'"]

    # with no limit there is no time for a wait to fit in, so a refusal is not retried
    api.requests.clear()
    api.script = [(503, {"Retry-After": "1"})]
    recorder, waits, logged = recorder_for(api, timeout=None)
    assert recorder.before_forward(REFUND_CALL).action_id is None
    assert waits == [] and len(api.requests) == 1 and "attempts" not in logged[0]

    given: list[float | None] = []

    def fake_post(api_url: str, api_key: str, body: dict[str, Any], *, timeout: float | None) -> dict[str, Any]:
        given.append(timeout)
        return {}

    monkeypatch.setattr(commitlayer_mcp_recorder, "post_action", fake_post)
    recorder, waits, logged = recorder_for(api, timeout=None)
    assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1" and given == [None]


def test_the_time_left_is_what_each_attempt_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[float] = []
    failures = [busy(429, 1.0), busy(503, 1.5)]

    def fake_post(api_url: str, api_key: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        given.append(timeout)
        if failures:
            raise failures.pop(0)
        return {}

    monkeypatch.setattr(commitlayer_mcp_recorder, "post_action", fake_post)
    clock = FakeClock()
    recorder = Recorder(
        load_mapping(MAPPING), api_key=TEST_KEY, sleep=clock.sleep, clock=clock, jitter=lambda: 0.0, log=lambda m: None
    )
    assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1"
    assert clock.waits == [1.0, 1.5] and given == [5.0, 4.0, 2.5]


def test_retries_can_be_turned_off(api: FakeApi, direct: None) -> None:
    api.script = [(503, {"Retry-After": "1"})]
    recorder, waits, logged = recorder_for(api, max_retries=0)
    assert recorder.before_forward(REFUND_CALL).action_id is None
    assert waits == [] and len(api.requests) == 1
    assert logged == [logged[0]] and "attempts" not in logged[0]
    with pytest.raises(ValueError):
        Recorder(load_mapping(MAPPING), max_retries=-1)


def test_the_wait_is_never_below_what_was_stated_and_the_addition_keeps_to_the_time_the_record_has() -> None:
    def waits_for(stated: float, spread: Any, timeout: float = 5.0) -> list[float]:
        recorder, waits, _ = recorder_for(
            None, transport=refusing(busy(retry_after=stated)), jitter=lambda: spread, timeout=timeout
        )
        assert recorder.before_forward(REFUND_CALL).action_id == "refund:o1"
        return waits

    # the stated second and a share of up to two more
    assert waits_for(1.0, 0.0) == [1.0]
    assert waits_for(1.0, 0.5) == [2.0]
    assert waits_for(1.0, 1.0) == [3.0]
    assert waits_for(1.0, 9.0) == [1.0] and waits_for(1.0, -1.0) == [1.0]
    assert waits_for(1.0, None) == [1.0] and waits_for(1.0, "x") == [1.0] and waits_for(1.0, float("nan")) == [1.0]
    # with 3.5 s for the record, a wait may take 2.5 and leave the attempt its second: the addition is a share
    # of the 1.5 s that allows, so the waits are still spread and the attempt after them still has its time
    assert waits_for(1.0, 1.0, timeout=3.5) == [2.5]
    assert waits_for(1.0, 0.5, timeout=3.5) == [1.75]
    assert waits_for(1.0, 0.0, timeout=3.5) == [1.0]
    assert waits_for(0.0, 1.0) == [0.0]


def test_a_transport_that_raises_anything_else_is_called_once() -> None:
    for failure in (RuntimeError("boom"), TimeoutError("timed out"), RecordingError("cannot reach the API")):
        calls: list[dict[str, Any]] = []
        logged: list[str] = []

        def transport(
            body: dict[str, Any], failure: Exception = failure, calls: list[dict[str, Any]] = calls
        ) -> dict[str, Any]:
            calls.append(body)
            raise failure

        recorder = Recorder(
            load_mapping(MAPPING),
            transport=transport,
            sleep=lambda s: pytest.fail("slept"),
            log=logged.append,
            max_retries=3,
        )
        decision = recorder.before_forward(REFUND_CALL)
        assert decision.forward and decision.action_id is None and len(calls) == 1
        assert logged == [f"action refund:o1 not recorded ({failure}); forwarding anyway"]


def test_retry_after_is_read_as_seconds_or_as_an_http_date() -> None:
    from datetime import datetime, timezone

    parse = commitlayer_mcp_recorder._parse_retry_after
    now = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)  # noqa: UP017 - Python 3.10
    assert [parse(v, now) for v in ("0", "1", "1.5", " 2 ", ".5")] == [0.0, 1.0, 1.5, 2.0, 0.5]
    assert parse("Fri, 13 Nov 2026 10:00:02 GMT", now) == 2.0
    assert parse("Fri, 13 Nov 2026 09:00:00 GMT", now) == 0.0
    for garbage in (None, "", "soon", "-1", "+1", "1e3", "NaN", "inf", "0x10", "1,5", "9" * 400):
        assert parse(garbage, now) is None


def test_cli_retries_a_refused_action_and_then_forwards_the_call(tmp_path: Path, api: FakeApi) -> None:
    api.script = [(429, {"Retry-After": "1"})]
    line = call(11, "refund_payment", {"order_id": "o11"})
    run = run_recorder(tmp_path, [line], api_url=api.url)
    assert len(api.requests) == 2 and api.requests[0].raw == api.requests[1].raw
    assert run.received_by_server == [line]
    assert run.response(11)["result"]["isError"] is False
    # the real spread: the stated second and up to two more
    line = re.compile(r"action refund:o11 refused \(HTTP 429\); retry 1 of 2 in [12]\.\d\d s")
    refused = [d for d in run.diagnostics if line.fullmatch(d)]
    assert len(refused) == 1
    assert "recorded refund:o11 for tool 'refund_payment'" in run.diagnostics
    assert TEST_KEY not in run.stderr


def test_cli_retries_zero_and_a_short_timeout_send_the_record_once(tmp_path: Path, api: FakeApi) -> None:
    api.script = [(503, {"Retry-After": "1"})]
    line = call(12, "refund_payment", {"order_id": "o12"})
    run = run_recorder(tmp_path, [line], api_url=api.url, args=["--retries", "0"])
    assert len(api.requests) == 1
    assert run.received_by_server == [line]
    assert any(d.startswith("action refund:o12 not recorded (HTTP 503") for d in run.diagnostics)

    # one second for the record and the API asks for one second: the wait does not fit, so there is no retry
    api.requests.clear()
    api.script = [(503, {"Retry-After": "1"})]
    short = tmp_path / "short"
    short.mkdir()
    run = run_recorder(short, [line], api_url=api.url, args=["--timeout", "1"])
    assert len(api.requests) == 1 and run.received_by_server == [line]
    assert not any("refused" in d for d in run.diagnostics)

    bad = run_recorder(short, [], api_url=api.url, args=["--retries", "-1"])
    assert bad.returncode == 2 and "--retries must be a whole number, 0 or more" in bad.stderr
