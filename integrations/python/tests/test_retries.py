"""What the integrations do when the API answers 429 or 503 with ``Retry-After``: the SDK client sends the same
body again after the stated wait, and the integrations that run on an event loop keep that wait off the loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import commitlayer_x402
from commitlayer import Client, CommitLayerError
from commitlayer_integrations import __version__
from commitlayer_integrations.tools import ToolRecorder

WORKSPACE_BUSY = {"error": {"code": "workspace_busy", "message": "7 writes in progress; not applied"}}
BUSY = {"error": {"code": "busy", "message": "The service is busy; the request was not applied."}}
MAPPING = {
    "refund_payment": {
        "contract_id": "refund_workflow",
        "subject": "arguments.order_id",
        "params": {"amount": "amount"},
        "action_id": "refund:{arguments.order_id}",
    },
    "close_ticket": {"contract_id": "ticket_workflow", "subject": "ticket_id"},  # no action_id template
}


class Refusing:
    """A transport that answers from a list of ``(status, payload, headers)`` and then records. It keeps every
    body as the bytes it would be on the wire and the thread it was called on."""

    def __init__(self, *refusals: tuple, before_answer=None):
        self.refusals = list(refusals)
        self.before_answer = before_answer
        self.sent: list[bytes] = []
        self.threads: list[int] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> tuple:
        self.sent.append(json.dumps(body).encode())
        self.threads.append(threading.get_ident())
        if self.before_answer:
            self.before_answer()
        if self.refusals:
            return self.refusals.pop(0)
        return 201, {"action": {"action_id": (body or {}).get("action_id")}}


def client_over(transport: Refusing, **options: Any) -> tuple[Client, list[float]]:
    waits: list[float] = []
    return Client(transport=transport, sleep=waits.append, jitter=lambda: 0.0, **options), waits


def test_version_constant_matches_the_package_version():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'(?m)^version = "([^"]+)"$', text)
    assert declared is not None and __version__ == declared.group(1)
    assert commitlayer_x402.__version__ == declared.group(1)  # the second constant in this package


def test_the_package_requires_the_sdk_that_retries():
    # The READMEs here describe the client's retries, and these tests use its options: an older SDK has neither.
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'(?m)^dependencies = \["commitlayer>=([0-9.]+)"\]$', text)
    assert floor is not None and tuple(int(n) for n in floor.group(1).split(".")) >= (0, 1, 3)


@pytest.mark.parametrize("refusal", [(429, WORKSPACE_BUSY, {"Retry-After": "1"}), (503, BUSY, {"retry-after": "1"})])
def test_tool_recorder_records_after_one_refusal_with_the_same_body(refusal):
    t = Refusing(refusal)
    client, waits = client_over(t)
    rec = ToolRecorder(client, MAPPING)
    out = rec.record("refund_payment", {"order_id": "o1", "amount": 5}, call_id="c1")
    assert out == {"action": {"action_id": "refund:o1"}} and rec.recorded == [out]
    assert waits == [1.0] and len(t.sent) == 2 and t.sent[0] == t.sent[1]

    # without a template and without a call id the recorder makes the id: once, not once per attempt
    t = Refusing(refusal)
    client, waits = client_over(t)
    out = ToolRecorder(client, MAPPING).record("close_ticket", {"ticket_id": "t9"})
    assert out is not None and re.fullmatch(r"agent:close_ticket:[0-9a-f]{32}", out["action"]["action_id"])
    assert waits == [1.0] and len(t.sent) == 2 and t.sent[0] == t.sent[1]


def test_tool_recorder_gives_up_with_the_last_refusal_under_the_fail_open_policy(caplog):
    refusal = (429, WORKSPACE_BUSY, {"Retry-After": "1"})
    t = Refusing(refusal, refusal, refusal, refusal, refusal)
    client, waits = client_over(t)  # the client's defaults: three retries
    assert ToolRecorder(client, MAPPING).record("refund_payment", {"order_id": "o1", "amount": 5}) is None
    assert waits == [1.0, 1.0, 1.0] and len(t.sent) == 4 and len(set(t.sent)) == 1
    assert "not recorded: workspace_busy (429)" in caplog.text

    t = Refusing(refusal, refusal)
    client, waits = client_over(t, max_retries=1)
    with pytest.raises(CommitLayerError) as e:
        ToolRecorder(client, MAPPING, fail_open=False).record("refund_payment", {"order_id": "o1", "amount": 5})
    assert (e.value.status, e.value.retry_after, e.value.attempts) == (429, 1.0, 2) and waits == [1.0]


def test_record_async_keeps_the_request_and_the_wait_off_the_event_loop():
    loop_thread: list[int] = []
    ticked = threading.Event()
    seen_tick: list[bool] = []
    # The transport waits for the loop to run another task. A recorder that blocked the loop would never see it.
    t = Refusing((503, BUSY, {"Retry-After": "1"}), before_answer=lambda: seen_tick.append(ticked.wait(timeout=5)))
    waited_on: list[int] = []
    client = Client(transport=t, sleep=lambda s: waited_on.append(threading.get_ident()), jitter=lambda: 0.0)
    told: list[tuple[int, dict[str, Any]]] = []
    rec = ToolRecorder(client, MAPPING, on_recorded=lambda out: told.append((threading.get_ident(), out)))

    async def main() -> dict[str, Any] | None:
        loop_thread.append(threading.get_ident())

        async def other_task() -> None:
            ticked.set()

        ticker = asyncio.ensure_future(other_task())
        out = await rec.record_async("refund_payment", {"order_id": "o1", "amount": 5}, call_id="c1")
        await ticker
        return out

    out = asyncio.run(main())
    assert out == {"action": {"action_id": "refund:o1"}} and rec.recorded == [out]
    assert seen_tick == [True, True]  # the loop ran its other task while both attempts were in flight
    assert len(t.sent) == 2 and t.sent[0] == t.sent[1]
    assert all(thread != loop_thread[0] for thread in t.threads + waited_on) and len(waited_on) == 1
    assert told == [(loop_thread[0], out)]  # the callback runs where the hook runs, not in the worker thread


def test_record_async_follows_the_same_fail_open_policy(caplog):
    t = Refusing((500, {"error": {"code": "internal_error", "message": "boom"}}, {"Retry-After": "1"}))
    client, waits = client_over(t)
    rec = ToolRecorder(client, MAPPING)
    assert asyncio.run(rec.record_async("refund_payment", {"order_id": "o1", "amount": 5})) is None
    assert waits == [] and len(t.sent) == 1 and "not recorded: internal_error (500)" in caplog.text
    assert asyncio.run(rec.record_async("search_docs", {"q": "x"})) is None and len(t.sent) == 1  # unmapped

    t = Refusing((500, {"error": {"code": "internal_error", "message": "boom"}}))
    client, waits = client_over(t)
    strict = ToolRecorder(client, MAPPING, fail_open=False)
    with pytest.raises(CommitLayerError):
        asyncio.run(strict.record_async("refund_payment", {"order_id": "o1", "amount": 5}))


def test_openai_agents_and_pydantic_ai_record_from_a_worker_thread_and_survive_a_refusal():
    from commitlayer_openai_agents import CommitLayerHooks
    from commitlayer_pydantic_ai import RecordingToolset

    t = Refusing((429, WORKSPACE_BUSY, {"Retry-After": "1"}))
    client, waits = client_over(t)
    hooks = CommitLayerHooks(ToolRecorder(client, MAPPING))
    ctx = SimpleNamespace(tool_name="refund_payment", tool_call_id="fc_1", tool_arguments='{"order_id": "o3", "amount": 1}')

    async def started() -> int:
        await hooks.on_tool_start(ctx, agent=None, tool=SimpleNamespace(name="refund_payment"))
        return len(t.sent)  # read on the loop, as the tool would start: the hook returned, so it is recorded

    assert asyncio.run(started()) == 2  # the refusal and the retry, both before the hook returned
    assert waits == [1.0] and len(t.sent) == 2 and t.sent[0] == t.sent[1]
    assert json.loads(t.sent[1])["action_id"] == "refund:o3"
    assert all(thread != threading.get_ident() for thread in t.threads)

    class Inner:
        async def call_tool(self, name, tool_args, ctx, tool):
            return f"ran {name} after {len(t.sent)} requests"

    t = Refusing((503, BUSY, {"Retry-After": "1"}))
    client, waits = client_over(t)
    toolset = RecordingToolset(Inner(), ToolRecorder(client, MAPPING))
    ctx = SimpleNamespace(tool_call_id="pyd_1", run_id="run_7")
    out = asyncio.run(toolset.call_tool("refund_payment", {"order_id": "o4", "amount": 2}, ctx, tool=None))
    assert out == "ran refund_payment after 2 requests"  # recorded, retry included, before the tool ran
    assert waits == [1.0] and t.sent[0] == t.sent[1]
    assert all(thread != threading.get_ident() for thread in t.threads)


def test_an_unmapped_tool_is_warned_about_once_whichever_method_meets_it(caplog):
    t = Refusing()
    client, _ = client_over(t)
    rec = ToolRecorder(client, MAPPING, warn_unmapped=True)
    with caplog.at_level(logging.WARNING, logger="commitlayer.integrations"):
        assert rec.record("search_docs", {"q": "x"}) is None
        assert asyncio.run(rec.record_async("search_docs", {"q": "y"})) is None
        assert asyncio.run(rec.record_async("list_orders", {})) is None
        assert rec.record("list_orders", {}) is None
        assert ToolRecorder(client, MAPPING).record("search_docs", {"q": "x"}) is None  # off by default
    assert [r.getMessage() for r in caplog.records] == [
        "commitlayer: tool 'search_docs' is not mapped; not recorded",
        "commitlayer: tool 'list_orders' is not mapped; not recorded",
    ]
    assert t.sent == []


def test_langgraph_async_wrapper_records_off_the_event_loop_before_the_tool_runs():
    from commitlayer_langgraph import record_tool_calls_async

    ticked = threading.Event()
    seen_tick: list[bool] = []
    # As above: the transport waits for the loop to run another task, which a wrapper on the loop would prevent.
    t = Refusing((429, WORKSPACE_BUSY, {"Retry-After": "1"}), before_answer=lambda: seen_tick.append(ticked.wait(timeout=5)))
    waited_on: list[int] = []
    client = Client(transport=t, sleep=lambda s: waited_on.append(threading.get_ident()), jitter=lambda: 0.0)
    rec = ToolRecorder(client, MAPPING)
    awrap = record_tool_calls_async(rec)
    assert inspect.iscoroutinefunction(awrap)  # what ToolNode's awrap_tool_call and LangChain's decorator look for
    request = SimpleNamespace(
        tool_call={"name": "refund_payment", "args": {"order_id": "o7", "amount": 3}, "id": "call_7"}, runtime=None
    )

    async def main() -> tuple[int, Any]:
        async def other_task() -> None:
            ticked.set()

        async def handler(req: Any) -> str:
            assert req is request
            return f"ran after {len(t.sent)} requests"

        ticker = asyncio.ensure_future(other_task())
        out = await awrap(request, handler)
        await ticker
        return threading.get_ident(), out

    loop_thread, out = asyncio.run(main())
    assert out == "ran after 2 requests"  # recorded, retry included, before the tool ran
    assert seen_tick == [True, True] and t.sent[0] == t.sent[1]
    body = json.loads(t.sent[0])
    assert body["action_id"] == "refund:o7" and body["channel"] == "langgraph"
    assert all(thread != loop_thread for thread in t.threads + waited_on) and len(waited_on) == 1

    # an unmapped tool and a request without a tool name go straight to the handler
    async def passthrough() -> list[Any]:
        async def handler(req: Any) -> str:
            return "ran"

        unmapped = SimpleNamespace(tool_call={"name": "search_docs", "args": {}, "id": "c"}, runtime=None)
        return [await awrap(unmapped, handler), await awrap(SimpleNamespace(tool_call=None), handler)]

    assert asyncio.run(passthrough()) == ["ran", "ran"] and len(t.sent) == 2


def test_a_tool_node_run_on_an_event_loop_records_through_the_async_wrapper():
    pytest.importorskip("langgraph.prebuilt", reason="langgraph is not installed (pip install langgraph)")
    from commitlayer_langgraph import record_tool_calls, record_tool_calls_async
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    @tool
    def refund_payment(order_id: str, amount: float) -> str:
        """Refund an order."""
        return "refunded"

    t = Refusing((429, WORKSPACE_BUSY, {"Retry-After": "1"}))
    waited_on: list[int] = []
    client = Client(transport=t, sleep=lambda s: waited_on.append(threading.get_ident()), jitter=lambda: 0.0)
    rec = ToolRecorder(client, MAPPING)
    graph = StateGraph(MessagesState)
    graph.add_node(
        "tools",
        ToolNode([refund_payment], wrap_tool_call=record_tool_calls(rec), awrap_tool_call=record_tool_calls_async(rec)),
    )
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()

    def calling(order_id: str) -> dict[str, Any]:
        call = {"name": "refund_payment", "args": {"order_id": order_id, "amount": 1.0}, "id": f"call_{order_id}"}
        return {"messages": [AIMessage(content="", tool_calls=[call])]}

    async def main() -> tuple[int, Any]:
        return threading.get_ident(), await app.ainvoke(calling("o8"))

    loop_thread, out = asyncio.run(main())
    assert out["messages"][-1].content == "refunded" and len(t.sent) == 2 and t.sent[0] == t.sent[1]
    assert all(thread != loop_thread for thread in t.threads + waited_on) and len(waited_on) == 1

    # run synchronously, the same node records through the synchronous wrapper, in the calling thread
    t.threads.clear()
    out = app.invoke(calling("o9"))
    assert out["messages"][-1].content == "refunded" and len(t.sent) == 3
    assert json.loads(t.sent[2])["action_id"] == "refund:o9"


def test_inngest_step_records_after_a_refusal_and_runs_fn_once():
    from commitlayer_inngest import recorded_step, recorded_step_async
    from test_inngest import RECORD, FakeCtx, FakeStep, FakeStepAsync

    t = Refusing((429, WORKSPACE_BUSY, {"Retry-After": "1"}))
    client, waits = client_over(t)
    ran: list[int] = []
    out = recorded_step(FakeCtx("run_1", FakeStep()), "refund", lambda: ran.append(len(t.sent)) or "done", client=client, record=RECORD)
    assert out == "done" and ran == [2] and waits == [1.0] and t.sent[0] == t.sent[1]
    assert json.loads(t.sent[1])["action_id"] == "inngest:run_1:refund"

    t = Refusing((503, BUSY, {"Retry-After": "1"}))
    client, waits = client_over(t)

    async def main() -> Any:
        ctx = FakeCtx("run_2", FakeStepAsync())
        return await recorded_step_async(ctx, "refund", lambda: "done", client=client, record=RECORD)

    assert asyncio.run(main()) == "done" and waits == [1.0] and t.sent[0] == t.sent[1]
    assert all(thread != threading.get_ident() for thread in t.threads)  # recorded in the executor, as before


def test_temporal_interceptor_records_after_a_refusal_off_the_worker_loop():
    pytest.importorskip("temporalio", reason="temporalio is not installed (pip install temporalio)")
    from commitlayer_temporal import CommitLayerInterceptor
    from test_temporal_interceptor import MAPPING as ACTIVITIES
    from test_temporal_interceptor import env_for, refund_payment, run_intercepted

    t = Refusing((429, WORKSPACE_BUSY, {"Retry-After": "1"}), (503, BUSY, {"Retry-After": "1"}))
    client, waits = client_over(t)
    result, nxt = run_intercepted(
        CommitLayerInterceptor(client, ACTIVITIES), env_for("refund_payment", activity_id="9"), refund_payment, "order_1", 180.0
    )  # fmt: skip
    assert result["refund_id"] == "re_1" and len(nxt.calls) == 1
    assert waits == [1.0, 1.0] and len(t.sent) == 3 and len(set(t.sent)) == 1
    assert json.loads(t.sent[0])["action_id"] == "temporal:wf-1:9"
    assert all(thread != threading.get_ident() for thread in t.threads)
