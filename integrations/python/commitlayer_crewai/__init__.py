"""CommitLayer for CrewAI: record a crew's tool calls as actions before the tools run.

CrewAI exposes a ``before_tool_call`` hook that fires with the tool's name and its input before every tool
call in the process. :func:`install` registers a recording hook built on
:class:`commitlayer_integrations.tools.ToolRecorder`; :func:`uninstall` removes it. The hook never blocks a
call and never alters its input or result: it declares the intent, and CommitLayer verifies the outcome
later from the systems of record.

    from commitlayer import Client
    from commitlayer_integrations.tools import ToolRecorder
    import commitlayer_crewai

    recorder = ToolRecorder(Client(agent_id="ops-crew"), MAPPING)
    commitlayer_crewai.install(recorder)          # before the crew kicks off
    crew.kickoff()
    recorder.wait("refund:order_10231")           # the completion gate, when the crew is done

``crewai`` is imported only inside :func:`install`, so this module imports without it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from commitlayer_integrations.tools import ToolRecorder

__all__ = ["install", "uninstall", "make_hook", "CHANNEL"]

CHANNEL = "crewai"


def make_hook(recorder: ToolRecorder) -> Callable[[Any], None]:
    """The ``before_tool_call`` hook: records the call from the context's ``tool_name`` and ``tool_input`` and
    returns ``None`` so the call proceeds. Exposed for tests and for callers who register hooks themselves."""
    if recorder.channel == "agent":
        recorder.channel = CHANNEL

    def before_tool_call(ctx: Any) -> None:
        name = getattr(ctx, "tool_name", None)
        args = getattr(ctx, "tool_input", None)
        if not name:
            return None
        task = getattr(ctx, "task", None)
        run_id = str(getattr(task, "id", "") or "") or None
        recorder.record(str(name), dict(args or {}), run_id=run_id)
        return None  # never block the call

    before_tool_call.__name__ = "commitlayer_before_tool_call"
    return before_tool_call


_installed: dict[int, Callable[[Any], None]] = {}


def install(
    recorder: ToolRecorder, *, tools: list[str] | None = None, agents: list[str] | None = None
) -> Callable[[Any], None]:
    """Register the recording hook with CrewAI (``crewai.hooks.register_before_tool_call_hook``). ``tools`` and
    ``agents`` narrow it to tool names or agent roles, as CrewAI's own decorator does; by default the hook sees
    every call and the recorder's mapping decides what is recorded."""
    from crewai.hooks import register_before_tool_call_hook  # imported here: optional dependency

    hook = make_hook(recorder)
    if tools or agents:
        # CrewAI's filtered registration goes through its decorator; apply it to the hook
        from crewai.hooks import before_tool_call

        hook = before_tool_call(tools=tools, agents=agents)(hook)
    else:
        register_before_tool_call_hook(hook)
    _installed[id(recorder)] = hook
    return hook


def uninstall(recorder: ToolRecorder) -> bool:
    """Remove the hook :func:`install` registered for this recorder. Returns whether one was removed."""
    hook = _installed.pop(id(recorder), None)
    if hook is None:
        return False
    from crewai.hooks import unregister_before_tool_call_hook

    return bool(unregister_before_tool_call_hook(hook))
