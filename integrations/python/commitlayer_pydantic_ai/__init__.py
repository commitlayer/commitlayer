"""CommitLayer for Pydantic AI: record tool calls as actions before the tools run.

Pydantic AI routes every tool call through a toolset's ``call_tool``; :class:`RecordingToolset` wraps any
toolset and records the mapped calls before delegating to it. The recorder sees the validated arguments and
the run's ``tool_call_id``; the tool's result is never sent.

    from pydantic_ai import Agent, FunctionToolset
    from commitlayer import Client
    from commitlayer_integrations.tools import ToolRecorder
    from commitlayer_pydantic_ai import RecordingToolset

    recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)
    tools = FunctionToolset(tools=[refund_payment, close_ticket])
    agent = Agent("openai:gpt-5", toolsets=[RecordingToolset(tools, recorder)])

``pydantic_ai`` is imported lazily: with it installed :class:`RecordingToolset` is a real ``WrapperToolset``;
without it the class keeps the same ``call_tool`` shape so this module imports and tests anywhere.
"""

from __future__ import annotations

from typing import Any

from commitlayer_integrations.tools import ToolRecorder

__all__ = ["RecordingToolset", "CHANNEL"]

CHANNEL = "pydantic_ai"

try:
    from pydantic_ai import WrapperToolset as _Base  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - optional dependency

    class _Base:  # type: ignore[no-redef]
        """Stand-in for ``pydantic_ai.WrapperToolset`` when Pydantic AI is not installed."""

        def __init__(self, wrapped: Any):
            self.wrapped = wrapped

        async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)


class RecordingToolset(_Base):  # type: ignore[misc]
    """A toolset wrapper that records every mapped tool call through ``recorder``, then delegates."""

    def __init__(self, wrapped: Any, recorder: ToolRecorder):
        super().__init__(wrapped)
        self.recorder = recorder
        if recorder.channel == "agent":
            recorder.channel = CHANNEL

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
        call_id = getattr(ctx, "tool_call_id", None)
        run_id = getattr(ctx, "run_id", None)
        self.recorder.record(name, dict(tool_args or {}), call_id=call_id, run_id=run_id)
        return await super().call_tool(name, tool_args, ctx, tool)
