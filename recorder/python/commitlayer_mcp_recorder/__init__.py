"""CommitLayer MCP Recorder: a stdio proxy that records an agent's MCP tool calls as CommitLayer actions.

The recorder sits between an MCP client (Claude Desktop, Claude Code, Cursor, an agent framework) and any
downstream MCP server. Every JSON-RPC message is forwarded unchanged in both directions. When the client
calls a tool that the mapping file names (``tools/call``), the intent is recorded as a CommitLayer action
**before** the call is forwarded, so the request time precedes every effect::

    commitlayer-mcp-recorder --map mapping.json -- npx -y @stripe/mcp --tools=all

The downstream result is never treated as proof: CommitLayer verifies from the systems of record. Recording
is fail-open by default: if the API cannot be reached the call is forwarded anyway and a warning goes to
stderr. The recorder never blocks, reorders or alters the agent's traffic.

Standard library only. ``load_mapping`` reads the mapping file, ``build_action`` turns one ``tools/call`` into
an action body and ``Recorder`` holds the policy (key, URL, fail-open) and the request ids still waiting for
a downstream answer.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_API_URL",
    "JSONRPC_RECORDING_FAILED",
    "LOG_PREFIX",
    "Decision",
    "Mapping",
    "MappingError",
    "Recorder",
    "RecordingError",
    "ToolMapping",
    "UnresolvedPath",
    "__version__",
    "build_action",
    "load_mapping",
    "log_to_stderr",
    "post_action",
    "resolve_path",
]
__version__ = "0.1.0"

DEFAULT_API_URL = "https://api.commitlayer.ai"
LOG_PREFIX = "commitlayer-recorder: "
JSONRPC_RECORDING_FAILED = -32000

Logger = Callable[[str], None]
Transport = Callable[[dict[str, Any]], dict[str, Any]]

_TEMPLATE_FIELD = re.compile(r"\{([^{}]+)\}")
_TOOL_STRING_FIELDS = ("action_type", "subject_kind", "action_id", "compensates")


def log_to_stderr(message: str) -> None:
    """Write one diagnostic line to stderr. Every line the recorder emits carries ``LOG_PREFIX``."""
    sys.stderr.write(f"{LOG_PREFIX}{message}\n")
    sys.stderr.flush()


class MappingError(ValueError):
    """The mapping file is missing, unreadable or does not describe tools the recorder can use."""


class UnresolvedPath(LookupError):
    """A path the mapping needs (the subject, a template field) is absent from the ``tools/call`` params."""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path

    def __str__(self) -> str:
        return self.path


class RecordingError(RuntimeError):
    """The API refused the action (HTTP 4xx/5xx) or could not be reached."""


# ---------------- mapping ----------------


@dataclass(frozen=True)
class ToolMapping:
    """How one MCP tool becomes an action. Paths are dotted paths into the ``tools/call`` params object
    (``arguments.order_id``, ``name``, ``_meta.progressToken``)."""

    contract_id: str
    subject: str
    action_type: str | None = None
    subject_kind: str | None = None
    contract_version: int | None = None
    params: dict[str, str] = field(default_factory=dict)
    action_id: str | None = None
    compensates: str | None = None


@dataclass(frozen=True)
class Mapping:
    tools: dict[str, ToolMapping]
    agent_id: str | None = None
    workflow_id: str | None = None

    def lookup(self, tool_name: str) -> ToolMapping | None:
        """The mapping for ``tool_name``: an exact key first, then a key carrying a server prefix
        (``stripe.refund_payment`` matches ``refund_payment``). Unmapped tools return ``None``."""
        exact = self.tools.get(tool_name)
        if exact is not None:
            return exact
        for key, tool in self.tools.items():
            _prefix, sep, bare = key.rpartition(".")
            if sep and bare == tool_name:
                return tool
        return None


def _optional_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MappingError(f"{where} must be a non-empty string")
    return value


def _required_str(obj: dict[str, Any], key: str, where: str) -> str:
    if key not in obj:
        raise MappingError(f"{where} is missing required key {key!r}")
    value = obj[key]
    if not isinstance(value, str) or not value:
        raise MappingError(f"{where}.{key} must be a non-empty string")
    return value


def _tool_mapping(name: str, spec: Any) -> ToolMapping:
    where = f"tools.{name}"
    if not isinstance(spec, dict):
        raise MappingError(f"{where} must be an object")
    params_spec = spec.get("params") or {}
    if not isinstance(params_spec, dict):
        raise MappingError(f"{where}.params must be an object of name -> path")
    params: dict[str, str] = {}
    for param_name, path in params_spec.items():
        if not isinstance(path, str) or not path:
            raise MappingError(f"{where}.params.{param_name} must be a non-empty path string")
        params[param_name] = path
    version = spec.get("contract_version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        raise MappingError(f"{where}.contract_version must be an integer")
    strings = {key: _optional_str(spec.get(key), f"{where}.{key}") for key in _TOOL_STRING_FIELDS}
    return ToolMapping(
        contract_id=_required_str(spec, "contract_id", where),
        subject=_required_str(spec, "subject", where),
        contract_version=version,
        params=params,
        **strings,
    )


def _read_mapping_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise MappingError(f"cannot read mapping file {path}: {e.strerror or e}") from e
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as e:
            raise MappingError("YAML mappings need PyYAML (pip install pyyaml); JSON works without it") from e
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise MappingError(f"mapping file {path} is not valid YAML: {e}") from e
    try:
        return json.loads(text)
    except ValueError as e:
        raise MappingError(f"mapping file {path} is not valid JSON: {e}") from e


def load_mapping(source: str | Path | dict[str, Any]) -> Mapping:
    """Load and validate a mapping from a JSON file, a YAML file (when PyYAML is installed) or a dict.

    Unknown keys are allowed. Every tool needs ``contract_id`` and ``subject``; ``params`` is an object of
    parameter name to path; ``action_id`` is a template with ``{path}`` placeholders.
    """
    raw = source if isinstance(source, dict) else _read_mapping_file(Path(source))
    if not isinstance(raw, dict):
        raise MappingError("mapping must be an object with a 'tools' key")
    tools_spec = raw.get("tools")
    if not isinstance(tools_spec, dict) or not tools_spec:
        raise MappingError("mapping needs a non-empty 'tools' object")
    tools = {str(name): _tool_mapping(str(name), spec) for name, spec in tools_spec.items()}
    return Mapping(
        tools=tools,
        agent_id=_optional_str(raw.get("agent_id"), "agent_id"),
        workflow_id=_optional_str(raw.get("workflow_id"), "workflow_id"),
    )


# ---------------- action bodies ----------------


def resolve_path(obj: Any, path: str) -> Any:
    """Follow a dotted path through nested objects and lists. Raises ``UnresolvedPath`` when any step is
    missing; a present ``null`` is returned as ``None``."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise UnresolvedPath(path)
    return current


def _render_template(template: str, call_params: dict[str, Any]) -> str:
    def substitute(match: re.Match[str]) -> str:
        value = resolve_path(call_params, match.group(1))
        if value is None:
            raise UnresolvedPath(match.group(1))
        return str(value)

    return _TEMPLATE_FIELD.sub(substitute, template)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_action(
    tool_name: str,
    call_params: dict[str, Any],
    tool: ToolMapping,
    *,
    request_id: Any,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    requested_at: str | None = None,
) -> dict[str, Any]:
    """The ``POST /v1/actions`` body for one ``tools/call``.

    ``call_params`` is the ``params`` object of the JSON-RPC request (``name``, ``arguments``, ``_meta``).
    ``params`` always carries ``mcp_tool`` and ``mcp_request_id``. The default ``action_id`` is
    ``mcp:{tool}:{request id}``, so a retried request updates one record instead of creating two. Raises
    ``UnresolvedPath`` when the subject or an ``action_id`` placeholder is absent from the call.
    """
    if "{" in tool.subject:
        # A template such as ``zendesk_ticket_{arguments.ticket_id}``: the connector's subject for a help-desk
        # ticket carries a prefix the tool call does not, so the mapping spells it out.
        subject = _render_template(tool.subject, call_params)
    else:
        subject = resolve_path(call_params, tool.subject)
    if subject is None or str(subject) == "":
        raise UnresolvedPath(tool.subject)
    params: dict[str, Any] = {}
    for name, path in tool.params.items():
        try:
            params[name] = resolve_path(call_params, path)
        except UnresolvedPath:
            continue
    params["mcp_tool"] = tool_name
    params["mcp_request_id"] = str(request_id)
    if tool.action_id is not None:
        action_id = _render_template(tool.action_id, call_params)
    else:
        action_id = f"mcp:{tool_name}:{request_id}"
    body: dict[str, Any] = {
        "action_id": action_id,
        "contract_id": tool.contract_id,
        "subject_id": str(subject),
        "requested_at": requested_at or _now(),
        "params": params,
        "channel": "mcp",
    }
    compensates: Any = None
    if tool.compensates is not None:
        try:
            compensates = resolve_path(call_params, tool.compensates)
        except UnresolvedPath:
            compensates = None
    for key, value in (
        ("contract_version", tool.contract_version),
        ("workflow_id", workflow_id),
        ("agent_id", agent_id),
        ("action_type", tool.action_type),
        ("subject_kind", tool.subject_kind),
        ("compensates", None if compensates is None else str(compensates)),
    ):
        if value is not None:
            body[key] = value
    return body


# ---------------- transport ----------------


def post_action(api_url: str, api_key: str, body: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """``POST {api_url}/v1/actions``. Returns the decoded response; raises ``RecordingError`` on HTTP 4xx/5xx
    or when the API cannot be reached. The key is sent in ``x-api-key`` and never logged."""
    req = urllib.request.Request(
        api_url.rstrip("/") + "/v1/actions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "x-api-key": api_key,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": f"commitlayer-mcp-recorder/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RecordingError(f"HTTP {e.code} from {req.full_url}: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", None) or e
        raise RecordingError(f"cannot reach {req.full_url}: {reason}") from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


# ---------------- recorder ----------------


@dataclass(frozen=True)
class Decision:
    """What to do with one client message: forward it, or (fail-closed only) answer the client with
    ``error`` instead. ``action_id`` is set when an action was recorded for the message."""

    forward: bool = True
    action_id: str | None = None
    error: dict[str, Any] | None = None


def _id_key(request_id: Any) -> str:
    # JSON-RPC ids may be numbers or strings and ``1`` is not ``"1"``; key on the JSON encoding.
    return json.dumps(request_id, sort_keys=True)


class Recorder:
    """Records mapped ``tools/call`` requests and matches downstream answers back to them.

    ``before_forward`` is called with every parsed client message before it is sent downstream and
    ``after_response`` with every parsed server message before it is sent back to the client. Both accept
    anything ``json.loads`` can return and ignore what is not a JSON-RPC object. Without an API key the
    recorder is a passthrough: nothing is posted. CLI flags for ``agent_id``/``workflow_id`` override the
    mapping file.
    """

    def __init__(
        self,
        mapping: Mapping,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        timeout: float = 5.0,
        fail_open: bool = True,
        warn_unmapped: bool = False,
        transport: Transport | None = None,
        log: Logger = log_to_stderr,
    ):
        self.mapping = mapping
        self.api_key = api_key or ""
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.agent_id = agent_id or mapping.agent_id
        self.workflow_id = workflow_id or mapping.workflow_id
        self.timeout = timeout
        self.fail_open = fail_open
        self.warn_unmapped = warn_unmapped
        self._transport = transport or self._post
        self._has_custom_transport = transport is not None
        self._log = log
        self._lock = threading.Lock()
        self._pending: dict[str, str] = {}
        self._warned_unmapped: set[str] = set()

    @property
    def enabled(self) -> bool:
        """``False`` without an API key: every call is forwarded and nothing is recorded."""
        return bool(self.api_key) or self._has_custom_transport

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        return post_action(self.api_url, self.api_key, body, timeout=self.timeout)

    def before_forward(self, message: Any) -> Decision:
        """Record the message if it is a mapped ``tools/call``; say whether to forward it."""
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return Decision()
        call_params = message.get("params")
        if not isinstance(call_params, dict) or not isinstance(call_params.get("name"), str):
            return Decision()
        tool_name: str = call_params["name"]
        tool = self.mapping.lookup(tool_name)
        if tool is None:
            if self.warn_unmapped and tool_name not in self._warned_unmapped:
                self._warned_unmapped.add(tool_name)
                self._log(f"unmapped tool {tool_name!r} forwarded without recording")
            return Decision()
        if "id" not in message:
            self._log(f"tools/call for {tool_name!r} has no JSON-RPC id; forwarded without recording")
            return Decision()
        if not self.enabled:
            return Decision()
        try:
            body = build_action(
                tool_name,
                call_params,
                tool,
                request_id=message["id"],
                agent_id=self.agent_id,
                workflow_id=self.workflow_id,
            )
        except UnresolvedPath as e:
            self._log(f"tool {tool_name!r} forwarded without recording: path {e} not found in the call")
            return Decision()
        action_id = body["action_id"]
        try:
            self._transport(body)
        except Exception as e:  # noqa: BLE001 - any failure to record is handled by the fail-open policy
            if self.fail_open:
                self._log(f"action {action_id} not recorded ({e}); forwarding anyway")
                return Decision()
            self._log(f"action {action_id} not recorded ({e}); call refused because fail-open is off")
            return Decision(
                forward=False,
                action_id=action_id,
                error={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": JSONRPC_RECORDING_FAILED,
                        "message": f"CommitLayer recording failed: {e}",
                    },
                },
            )
        with self._lock:
            self._pending[_id_key(message["id"])] = action_id
        self._log(f"recorded {action_id} for tool {tool_name!r}")
        return Decision(action_id=action_id)

    def after_response(self, message: Any) -> str | None:
        """Match a downstream answer to a recorded request and log its ``isError`` flag. The result itself is
        never sent to the API: a tool's own report of success is not evidence."""
        if not isinstance(message, dict) or "id" not in message or "method" in message:
            return None
        with self._lock:
            action_id = self._pending.pop(_id_key(message["id"]), None)
        if action_id is None:
            return None
        result = message.get("result")
        is_error = bool(result.get("isError")) if isinstance(result, dict) else "error" in message
        self._log(f"recorded {action_id} → tool result isError={'true' if is_error else 'false'}")
        return action_id

    def pending(self) -> dict[str, str]:
        """Recorded requests still waiting for a downstream answer: JSON-RPC id (encoded) → action id."""
        with self._lock:
            return dict(self._pending)
