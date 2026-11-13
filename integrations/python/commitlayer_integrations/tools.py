"""Record an agent framework's tool call as a CommitLayer action, before the tool runs.

The framework adapters (``commitlayer_langgraph``, ``commitlayer_crewai``, ``commitlayer_openai_agents``,
``commitlayer_pydantic_ai``) all do the same thing at different hook points: when the agent is about to call a
tool whose name is mapped, post the intent as an action and let the call proceed. This module is that one
thing, with no framework imported, so the body a tool call produces can be asserted in a unit test.

The agent may declare its intent. It may read the verdict. It must never control the independent facts, the
governing contract, or the final evidence. Recording is fail-open by default: a verifier that is down must not
stop the business action, and the tool's own result is never sent anywhere.

Mapping
-------
``tools`` maps a tool name to what to record for it, in the same shape the MCP recorder's mapping file uses::

    {
      "refund_payment": {
        "contract_id": "refund_workflow",
        "action_type": "refund_order",
        "subject": "arguments.order_id",
        "subject_kind": "order",
        "params": {"amount": "arguments.amount", "currency": "arguments.currency"},
        "action_id": "refund:{arguments.order_id}"
      }
    }

Paths (``subject``, every ``params`` value, ``compensates``) are dotted paths into the call: ``arguments.x.y``
reads the tool's arguments; a bare ``x`` is shorthand for ``arguments.x``; ``tool``, ``call_id`` and
``run_id`` are the call's own fields. ``action_id`` is a template whose ``{...}`` placeholders are the same
paths; without one the id is ``{channel}:{tool}:{call_id}`` so a retried call updates the one record.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("commitlayer.integrations")

__all__ = ["ToolMapping", "ToolRecorder", "MappingError", "build_tool_action", "resolve_path"]


class MappingError(LookupError):
    """A path or template could not be resolved against the tool call."""


@dataclass(frozen=True)
class ToolMapping:
    contract_id: str
    subject: str
    params: Mapping[str, str] = field(default_factory=dict)
    action_id: str | None = None
    action_type: str | None = None
    subject_kind: str | None = None
    compensates: str | None = None
    contract_version: int | None = None

    @classmethod
    def coerce(cls, value: ToolMapping | Mapping[str, Any]) -> ToolMapping:
        if isinstance(value, ToolMapping):
            return value
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise MappingError(f"unknown mapping keys: {sorted(unknown)}")
        if "contract_id" not in value or "subject" not in value:
            raise MappingError("a tool mapping needs contract_id and subject")
        return cls(**dict(value))


def _step(value: Any, key: str, path: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        raise MappingError(f"{path!r}: no key {key!r}")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        try:
            return value[int(key)]
        except (ValueError, IndexError) as e:
            raise MappingError(f"{path!r}: no index {key!r}") from e
    if hasattr(value, key):
        return getattr(value, key)
    raise MappingError(f"{path!r}: {type(value).__name__} has no attribute {key!r}")


def resolve_path(path: str, call: Mapping[str, Any]) -> Any:
    """``arguments.x.y`` into the call; ``x.y`` means ``arguments.x.y``; ``tool``/``call_id``/``run_id`` are the
    call's own fields."""
    if not isinstance(path, str) or not path:
        raise MappingError(f"path must be a non-empty string, got {path!r}")
    parts = path.split(".")
    if parts[0] not in ("arguments", "tool", "call_id", "run_id"):
        parts = ["arguments", *parts]
    value: Any = call
    for key in parts:
        value = _step(value, key, path)
    return value


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 - Python 3.10


def build_tool_action(
    mapping: ToolMapping | Mapping[str, Any],
    *,
    tool: str,
    arguments: Mapping[str, Any] | None,
    call_id: str | None = None,
    run_id: str | None = None,
    channel: str = "agent",
    agent_id: str | None = None,
    workflow_id: str | None = None,
    requested_at: str | None = None,
) -> dict[str, Any]:
    """The exact ``POST /v1/actions`` body for one tool call. Pure."""
    m = ToolMapping.coerce(mapping)
    call = {
        "tool": tool,
        "arguments": dict(arguments or {}),
        "call_id": call_id or "",
        "run_id": run_id or "",
    }
    subject = resolve_path(m.subject, call)
    if subject is None or subject == "":
        raise MappingError(f"subject path {m.subject!r} resolved to nothing")
    params = {name: resolve_path(p, call) for name, p in m.params.items()}
    if m.action_id:
        action_id = _PLACEHOLDER.sub(lambda mm: str(resolve_path(mm.group(1), call)), m.action_id)
    else:
        action_id = f"{channel}:{tool}:{call_id or uuid.uuid4().hex}"
    if not action_id or len(action_id) > 120:
        raise MappingError(f"action_id {action_id!r} must be 1-120 characters")
    body: dict[str, Any] = {
        "action_id": action_id,
        "contract_id": m.contract_id,
        "subject_id": str(subject),
        "requested_at": requested_at or _now(),
        "params": {**params, "tool": tool, **({"tool_call_id": call_id} if call_id else {})},
        "channel": channel,
    }
    for k, v in (
        ("agent_id", agent_id),
        ("workflow_id", workflow_id or (run_id or None)),
        ("action_type", m.action_type),
        ("subject_kind", m.subject_kind),
        ("compensates", resolve_path(m.compensates, call) if m.compensates else None),
        ("contract_version", m.contract_version),
    ):
        if v is not None:
            body[k] = v
    return body


class ToolRecorder:
    """Records mapped tool calls through a :class:`commitlayer.Client`.

    ``client`` is the SDK client holding a *recorder* key. ``tools`` is the mapping described in the module
    docstring; a key with a server prefix (``stripe.refund_payment``) also matches the bare name. ``channel``
    names the framework and is stored on every action. With ``fail_open`` (the default) a failure to record is
    logged and the tool call goes ahead; with ``fail_open=False`` the exception reaches the caller, which
    frameworks turn into a tool error. ``on_recorded`` receives the API's answer (``{action, verification,
    allowance}``) for every recorded call.

    The client sends a recording again when the API answers 429 or 503 with ``Retry-After`` (see the SDK's
    README), and it waits with a blocking sleep. :meth:`record` therefore belongs in synchronous hooks;
    :meth:`record_async` is the same call for a hook that runs on an event loop.
    """

    def __init__(
        self,
        client: Any,
        tools: Mapping[str, ToolMapping | Mapping[str, Any]],
        *,
        channel: str = "agent",
        agent_id: str | None = None,
        workflow_id: str | None = None,
        fail_open: bool = True,
        warn_unmapped: bool = False,
        on_recorded: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.client = client
        self.tools = {name: ToolMapping.coerce(m) for name, m in tools.items()}
        self.channel = channel
        self.agent_id = agent_id
        self.workflow_id = workflow_id
        self.fail_open = fail_open
        self.warn_unmapped = warn_unmapped
        self.on_recorded = on_recorded
        self.recorded: list[dict[str, Any]] = []  # the API's answers, newest last
        self._warned: set[str] = set()

    def mapping_for(self, tool: str) -> ToolMapping | None:
        if tool in self.tools:
            return self.tools[tool]
        for name, m in self.tools.items():
            if "." in name and name.rsplit(".", 1)[1] == tool:
                return m
        return None

    def record(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        *,
        call_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record one tool call if its name is mapped. Returns the API's answer, or ``None`` when the tool is
        unmapped or (fail-open) recording failed. Blocks for the request and for any wait before a retry."""
        m = self._mapped(tool)
        if m is None:
            return None
        try:
            out = self._post(m, tool, arguments, call_id, run_id)
        except Exception as e:  # noqa: BLE001 - the business action must not depend on the verifier
            if not self.fail_open:
                raise
            log.warning("commitlayer: tool call %r not recorded: %s", tool, e)
            return None
        return self._keep(out)

    async def record_async(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        *,
        call_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """:meth:`record` for a hook that runs on an event loop. The request, and the wait the API may ask for
        before a retry, happen in a worker thread, so the loop keeps serving its other tasks; the call is
        still recorded before this returns. ``on_recorded`` runs on the loop's thread, as with :meth:`record`."""
        m = self._mapped(tool)
        if m is None:
            return None
        try:
            out = await asyncio.to_thread(self._post, m, tool, arguments, call_id, run_id)
        except Exception as e:  # noqa: BLE001 - the business action must not depend on the verifier
            if not self.fail_open:
                raise
            log.warning("commitlayer: tool call %r not recorded: %s", tool, e)
            return None
        return self._keep(out)

    def _mapped(self, tool: str) -> ToolMapping | None:
        m = self.mapping_for(tool)
        if m is None and self.warn_unmapped and tool not in self._warned:
            self._warned.add(tool)
            log.warning("commitlayer: tool %r is not mapped; not recorded", tool)
        return m

    def _post(
        self,
        m: ToolMapping,
        tool: str,
        arguments: Mapping[str, Any] | None,
        call_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        # The body is built once, so a retry inside the client repeats the same action_id and request time.
        body = build_tool_action(
            m,
            tool=tool,
            arguments=arguments,
            call_id=call_id,
            run_id=run_id,
            channel=self.channel,
            agent_id=self.agent_id,
            workflow_id=self.workflow_id,
        )
        return self.client.request("POST", "/v1/actions", body)

    def _keep(self, out: dict[str, Any]) -> dict[str, Any]:
        self.recorded.append(out)
        if self.on_recorded:
            self.on_recorded(out)
        return out

    def wait(self, action_id: str, *, timeout: float = 300.0) -> dict[str, Any]:
        """The settled verification for a recorded action (``GET /v1/actions/{id}/wait``), through the SDK."""
        return self.client.wait_for_settlement(action_id, timeout=timeout)
