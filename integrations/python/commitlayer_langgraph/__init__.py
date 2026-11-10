"""CommitLayer for LangGraph and LangChain agents: record a tool call as an action before the tool runs.

Two entry points, both built on :class:`commitlayer_integrations.tools.ToolRecorder`:

* :func:`record_tool_calls` returns a ``wrap_tool_call`` function for ``langgraph.prebuilt.ToolNode`` and for
  the LangChain v1 ``create_agent`` middleware list. It records the mapped call and then hands it to the
  framework's handler unchanged; the tool's result is never sent to CommitLayer.
* :func:`verdict_gate_node` returns a graph node that waits for a recorded action's verdict to settle and
  writes it into the state, so the graph branches on PASS / FAIL / UNKNOWN instead of on the tool's own word.

Nothing here imports LangGraph or LangChain: the wrapper and the node are plain callables with the shapes
those libraries call, so this module works with any version that keeps them, and can be unit-tested without
either installed.

    from commitlayer import Client
    from commitlayer_langgraph import record_tool_calls
    from langgraph.prebuilt import ToolNode

    recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)
    tools = ToolNode([refund_payment, close_ticket], wrap_tool_call=record_tool_calls(recorder))

    # or, with LangChain v1 agents:
    from langchain.agents import create_agent
    agent = create_agent(model, tools=[...], middleware=[record_tool_calls(recorder)])
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from commitlayer_integrations.tools import ToolRecorder

__all__ = ["record_tool_calls", "verdict_gate_node", "CHANNEL"]

CHANNEL = "langgraph"


def _call_fields(request: Any) -> tuple[str, dict[str, Any], str | None, str | None]:
    """Tool name, arguments, tool call id and run id from a ``ToolCallRequest`` (or anything shaped like one)."""
    tc = getattr(request, "tool_call", None) or {}
    name = tc.get("name") if isinstance(tc, Mapping) else getattr(tc, "name", None)
    args = tc.get("args") if isinstance(tc, Mapping) else getattr(tc, "args", None)
    call_id = tc.get("id") if isinstance(tc, Mapping) else getattr(tc, "id", None)
    run_id = None
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None) if runtime is not None else None
    if isinstance(config, Mapping):
        run_id = (config.get("metadata") or {}).get("thread_id") or (config.get("configurable") or {}).get(
            "thread_id"
        )
    return str(name or ""), dict(args or {}), call_id, run_id


def record_tool_calls(recorder: ToolRecorder) -> Callable[[Any, Callable[[Any], Any]], Any]:
    """A ``wrap_tool_call(request, handler)`` function that records the mapped call, then runs it.

    Works as the ``wrap_tool_call`` argument of ``ToolNode`` and as an item of ``create_agent``'s
    ``middleware`` list (LangChain accepts a bare function there through its ``@wrap_tool_call`` decorator;
    pass ``langchain.agents.middleware.wrap_tool_call(record_tool_calls(recorder))`` on versions that require
    the decorator). The recorder's ``channel`` is set to ``langgraph`` unless it already names something else.
    """
    if recorder.channel == "agent":
        recorder.channel = CHANNEL

    def wrap_tool_call(request: Any, handler: Callable[[Any], Any]) -> Any:
        name, args, call_id, run_id = _call_fields(request)
        if name:
            recorder.record(name, args, call_id=call_id, run_id=run_id)
        return handler(request)

    wrap_tool_call.__name__ = "commitlayer_record_tool_calls"
    return wrap_tool_call


def verdict_gate_node(
    recorder: ToolRecorder,
    *,
    action_id: Callable[[Mapping[str, Any]], str | None] | str = "commitlayer_action_id",
    key: str = "commitlayer_verdict",
    timeout: float = 300.0,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A node that waits for a recorded action's verdict to settle and writes it into the state.

    ``action_id`` is the state key holding the action id (default ``commitlayer_action_id``) or a callable
    over the state returning it; when it is missing, the node uses the last action the recorder recorded.
    The node returns ``{key: {"status", "outcome", "policy", "headline", "verification"}}``; a conditional
    edge then routes PASS to the happy path and FAIL or UNKNOWN to recovery. A timeout is reported as
    ``status: "pending"`` and is the graph's decision, never a success.
    """

    def node(state: Mapping[str, Any]) -> dict[str, Any]:
        aid = action_id(state) if callable(action_id) else state.get(action_id)
        if not aid and recorder.recorded:
            aid = (recorder.recorded[-1].get("action") or {}).get("action_id")
        if not aid:
            return {key: {"status": "unrecorded", "outcome": None, "policy": None, "headline": None}}
        v = recorder.wait(str(aid), timeout=timeout)
        lenses = v.get("lenses") or {}
        return {
            key: {
                "status": "pending" if v.get("timed_out") else v.get("status"),
                "outcome": (lenses.get("outcome") or {}).get("verdict"),
                "policy": (lenses.get("policy") or {}).get("verdict"),
                "headline": (v.get("summary") or {}).get("headline"),
                "verification": v,
            }
        }

    node.__name__ = "commitlayer_verdict_gate"
    return node
