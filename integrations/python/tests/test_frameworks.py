"""The four framework adapters, each driven with the shapes its framework passes, none of the frameworks
installed: the mapped call is recorded before the tool runs, the tool still runs, its result goes nowhere."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from commitlayer_integrations.tools import ToolRecorder

MAPPING = {
    "refund_payment": {
        "contract_id": "refund_workflow",
        "subject": "arguments.order_id",
        "params": {"amount": "amount"},
        "action_id": "refund:{arguments.order_id}",
    }
}


def test_langgraph_wrapper_records_then_runs_the_handler(client, transport):
    from commitlayer_langgraph import record_tool_calls, verdict_gate_node

    rec = ToolRecorder(client, MAPPING)
    wrap = record_tool_calls(rec)
    request = SimpleNamespace(
        tool_call={"name": "refund_payment", "args": {"order_id": "o1", "amount": 9}, "id": "call_1"},
        tool=None,
        state={},
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "thread_9"}}),
    )
    seen = []
    out = wrap(request, lambda r: seen.append(r) or "tool-result")
    assert out == "tool-result" and seen == [request]
    posted = transport.posted_actions[-1]
    assert posted["action_id"] == "refund:o1" and posted["channel"] == "langgraph"
    assert posted["workflow_id"] == "thread_9" and posted["params"]["tool_call_id"] == "call_1"
    # an unmapped tool runs without a record
    other = SimpleNamespace(
        tool_call={"name": "search", "args": {}, "id": "c2"}, tool=None, state={}, runtime=None
    )
    assert wrap(other, lambda r: "ok") == "ok" and len(transport.posted_actions) == 1

    # the gate node reads the verdict of the last recorded action
    transport.handler = lambda m, p, b: (
        200,
        {
            "status": "settled",
            "timed_out": False,
            "lenses": {"outcome": {"verdict": "PASS"}, "policy": {"verdict": "FAIL"}},
            "summary": {"headline": "FAIL — amount above 5"},
        },
    )
    gate = verdict_gate_node(rec, timeout=5)
    verdict = gate({})["commitlayer_verdict"]
    assert verdict["status"] == "settled" and verdict["policy"] == "FAIL" and "above 5" in verdict["headline"]
    assert transport.calls[-1][1].startswith("/v1/actions/refund:o1/wait")


def test_crewai_hook_records_from_the_context_and_never_blocks(client, transport):
    from commitlayer_crewai import make_hook

    rec = ToolRecorder(client, MAPPING)
    hook = make_hook(rec)
    ctx = SimpleNamespace(
        tool_name="refund_payment",
        tool_input={"order_id": "o2", "amount": 3},
        task=SimpleNamespace(id="task_1"),
    )
    assert hook(ctx) is None
    posted = transport.posted_actions[-1]
    assert (
        posted["channel"] == "crewai" and posted["subject_id"] == "o2" and posted["workflow_id"] == "task_1"
    )
    assert hook(SimpleNamespace(tool_name="browse", tool_input={})) is None
    assert len(transport.posted_actions) == 1


def test_openai_agents_hooks_record_function_tools_from_the_tool_context(client, transport):
    from commitlayer_openai_agents import CommitLayerHooks

    rec = ToolRecorder(client, MAPPING)
    hooks = CommitLayerHooks(rec)
    tool = SimpleNamespace(name="refund_payment")
    ctx = SimpleNamespace(
        tool_name="refund_payment", tool_call_id="fc_1", tool_arguments='{"order_id": "o3", "amount": 1}'
    )
    asyncio.run(hooks.on_tool_start(ctx, agent=None, tool=tool))
    posted = transport.posted_actions[-1]
    assert posted["channel"] == "openai_agents" and posted["subject_id"] == "o3"
    assert posted["params"] == {"amount": 1, "tool": "refund_payment", "tool_call_id": "fc_1"}
    # a hosted tool passes a context without arguments: nothing to map, nothing recorded
    asyncio.run(hooks.on_tool_start(SimpleNamespace(), agent=None, tool=SimpleNamespace(name="web_search")))
    asyncio.run(hooks.on_tool_end(ctx, agent=None, tool=tool, result={"ok": True}))
    assert len(transport.posted_actions) == 1


def test_pydantic_ai_toolset_records_then_delegates(client, transport):
    from commitlayer_pydantic_ai import RecordingToolset

    class Inner:
        async def call_tool(self, name, tool_args, ctx, tool):
            return f"ran {name}"

    rec = ToolRecorder(client, MAPPING)
    ts = RecordingToolset(Inner(), rec)
    ctx = SimpleNamespace(tool_call_id="pyd_1", run_id="run_7")
    out = asyncio.run(ts.call_tool("refund_payment", {"order_id": "o4", "amount": 2}, ctx, tool=None))
    assert out == "ran refund_payment"
    posted = transport.posted_actions[-1]
    assert (
        posted["channel"] == "pydantic_ai"
        and posted["subject_id"] == "o4"
        and posted["workflow_id"] == "run_7"
    )
