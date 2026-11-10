"""CommitLayer for the OpenAI Agents SDK: record function-tool calls as actions before they run.

:class:`CommitLayerHooks` is a ``RunHooks`` whose ``on_tool_start`` records the mapped call. The SDK passes
function tools a ``ToolContext`` carrying ``tool_name``, ``tool_call_id`` and the raw ``tool_arguments`` JSON,
which is everything the recorder needs; hosted tools (web search, computer use) pass a plain context and are
left alone. The hook never blocks the call and never reads its result.

    from agents import Agent, Runner
    from commitlayer import Client
    from commitlayer_integrations.tools import ToolRecorder
    from commitlayer_openai_agents import CommitLayerHooks

    recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)
    result = await Runner.run(agent, "Refund order 10231", hooks=CommitLayerHooks(recorder))
    recorder.wait("refund:order_10231")           # the completion gate

``agents`` (the SDK) is imported lazily: when it is installed, :class:`CommitLayerHooks` subclasses its
``RunHooks`` so the SDK's type checks pass; when it is not, the class stands alone with the same methods, so
this module imports and tests anywhere.
"""

from __future__ import annotations

import json
from typing import Any

from commitlayer_integrations.tools import ToolRecorder

__all__ = ["CommitLayerHooks", "CHANNEL"]

CHANNEL = "openai_agents"

try:  # the SDK's base class when available, so the hooks object is a RunHooks to the SDK
    from agents import RunHooks as _Base  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - optional dependency

    class _Base:  # type: ignore[no-redef]
        """Stand-in for ``agents.RunHooks`` when the SDK is not installed."""


def _arguments(context: Any) -> dict[str, Any]:
    raw = getattr(context, "tool_arguments", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            return {"raw": raw}
    return {}


class CommitLayerHooks(_Base):  # type: ignore[misc]
    """``RunHooks`` that records every mapped tool call through ``recorder`` before the tool runs."""

    def __init__(self, recorder: ToolRecorder):
        super().__init__()
        self.recorder = recorder
        if recorder.channel == "agent":
            recorder.channel = CHANNEL

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        name = getattr(context, "tool_name", None) or getattr(tool, "name", None)
        if not name or not hasattr(context, "tool_arguments"):
            return  # a hosted tool with no arguments to map
        call_id = getattr(context, "tool_call_id", None)
        self.recorder.record(str(name), _arguments(context), call_id=call_id)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        return None  # the tool's own result is never evidence
