"""The Inngest helpers against fake step/context objects. No Inngest dev server, no ``inngest`` import needed."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from commitlayer import Client
from commitlayer_inngest import (
    CommitLayerSteps,
    RecordSpec,
    build_action_body,
    recorded_step,
    recorded_step_async,
    wait_for_verdict,
    wait_for_verdict_async,
)
from conftest import FakeTransport, verification


class FakeStep:
    """Memoises like Inngest: a step id seen before returns its stored output without running the handler."""

    def __init__(self, memos: dict[str, Any] | None = None) -> None:
        self.memos: dict[str, Any] = dict(memos or {})
        self.ran: list[str] = []
        self.sleeps: list[tuple[str, timedelta]] = []

    def run(self, step_id: str, handler, *args):
        if step_id in self.memos:
            return self.memos[step_id]
        self.ran.append(step_id)
        out = handler(*args)
        self.memos[step_id] = out
        return out

    def sleep(self, step_id: str, duration) -> None:
        self.sleeps.append((step_id, duration))


class FakeStepAsync(FakeStep):
    async def run(self, step_id: str, handler, *args):  # type: ignore[override]
        if step_id in self.memos:
            return self.memos[step_id]
        self.ran.append(step_id)
        out = handler(*args)
        if asyncio.iscoroutine(out):
            out = await out
        self.memos[step_id] = out
        return out

    async def sleep(self, step_id: str, duration) -> None:  # type: ignore[override]
        self.sleeps.append((step_id, duration))


@dataclass
class FakeCtx:
    run_id: str
    step: Any
    event: dict[str, Any] = field(default_factory=dict)


RECORD = {
    "contract_id": "refund_workflow",
    "subject_id": "order_10231",
    "params": {"amount": 180.0, "reason": "customer_cancelled"},
    "subject_kind": "order",
    "action_type": "refund_order",
}


# ---- body ----


def test_body_is_exactly_what_post_v1_actions_accepts():
    body = build_action_body(
        {**RECORD, "contract_version": 3, "compensates": "inngest:run_0:refund"},
        run_id="run_1",
        step_id="refund",
        agent_id="support-agent",
        workflow_id="order-refund",
        requested_at="2026-10-15T10:42:00Z",
    )
    assert body == {
        "action_id": "inngest:run_1:refund",
        "contract_id": "refund_workflow",
        "subject_id": "order_10231",
        "requested_at": "2026-10-15T10:42:00Z",
        "params": {"amount": 180.0, "reason": "customer_cancelled", "inngest_run_id": "run_1"},
        "channel": "inngest",
        "workflow_id": "order-refund",
        "agent_id": "support-agent",
        "action_type": "refund_order",
        "subject_kind": "order",
        "compensates": "inngest:run_0:refund",
        "contract_version": 3,
    }


def test_body_record_values_win_over_defaults_and_template_works():
    spec = RecordSpec(
        "c", "s1", params={"amount": 1}, workflow_id="wf", agent_id="a", action_id="r:{subject_id}:{amount}"
    )
    body = build_action_body(spec, run_id="r", step_id="x", agent_id="other", workflow_id="other")
    assert body["workflow_id"] == "wf" and body["agent_id"] == "a" and body["action_id"] == "r:s1:1"
    minimal = build_action_body({"contract_id": "c", "subject_id": "s"}, run_id="r", step_id="x")
    assert set(minimal) == {"action_id", "contract_id", "subject_id", "requested_at", "params", "channel"}
    assert minimal["params"] == {"inngest_run_id": "r"}


def test_body_rejects_bad_specs():
    with pytest.raises(ValueError, match="unknown record keys"):
        build_action_body({"contract_id": "c", "subject_id": "s", "result": 1}, run_id="r", step_id="x")
    with pytest.raises(ValueError, match="subject_id"):
        build_action_body({"contract_id": "c", "subject_id": ""}, run_id="r", step_id="x")
    with pytest.raises(ValueError, match="unknown placeholder"):
        build_action_body(
            {"contract_id": "c", "subject_id": "s", "action_id": "{nope}"}, run_id="r", step_id="x"
        )


# ---- recorded_step (sync) ----


def test_recorded_step_records_inside_the_step_then_runs_fn(client: Client, transport: FakeTransport):
    order: list[str] = []
    transport.handler = lambda m, p, b: (order.append("recorded"), (201, {"action": b}))[1]
    ctx = FakeCtx("run_1", FakeStep())

    def refund(order_id: str, amount: float) -> dict:
        order.append("ran")
        return {"refund_id": "re_1", "amount": amount}

    out = recorded_step(
        ctx, "refund", refund, "order_10231", 180.0, client=client, record=RECORD, agent_id="bot"
    )
    assert out == {"refund_id": "re_1", "amount": 180.0}
    assert order == ["recorded", "ran"] and ctx.step.ran == ["refund"]
    [body] = transport.posted_actions
    assert body["action_id"] == "inngest:run_1:refund" and body["channel"] == "inngest"
    assert body["params"]["inngest_run_id"] == "run_1" and body["agent_id"] == "bot"
    assert "refund_id" not in str(body)  # the step's result never leaves


def test_replayed_step_is_memoised_and_not_re_recorded(client: Client, transport: FakeTransport):
    step = FakeStep(memos={"refund": {"refund_id": "re_1"}})
    ctx = FakeCtx("run_1", step)
    calls = []
    out = recorded_step(ctx, "refund", lambda: calls.append(1), client=client, record=RECORD)
    assert out == {"refund_id": "re_1"} and calls == [] and transport.calls == []


def test_retry_of_an_unfinished_step_records_the_same_action_id(client: Client, transport: FakeTransport):
    ctx = FakeCtx("run_1", FakeStep())
    attempts = [0]

    def flaky():
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("transient")
        return "ok"

    with pytest.raises(RuntimeError):
        recorded_step(ctx, "refund", flaky, client=client, record=RECORD)
    assert recorded_step(ctx, "refund", flaky, client=client, record=RECORD) == "ok"
    ids = {b["action_id"] for b in transport.posted_actions}
    assert len(transport.posted_actions) == 2 and ids == {"inngest:run_1:refund"}


def test_fail_open_logs_and_runs_fn(client: Client, transport: FakeTransport, caplog):
    transport.handler = lambda m, p, b: (503, {"error": {"code": "unavailable", "message": "down"}})
    ctx = FakeCtx("run_1", FakeStep())
    with caplog.at_level(logging.WARNING, logger="commitlayer.inngest"):
        out = recorded_step(ctx, "refund", lambda: "done", client=client, record=RECORD)
    assert out == "done"
    assert any("action not recorded for step refund" in r.message for r in caplog.records)


def test_fail_closed_raises_inside_the_step_before_fn(client: Client, transport: FakeTransport):
    from commitlayer import CommitLayerError

    transport.handler = lambda m, p, b: (503, {"error": {"code": "unavailable", "message": "down"}})
    ctx = FakeCtx("run_1", FakeStep())
    ran = []
    with pytest.raises(CommitLayerError):
        recorded_step(ctx, "refund", lambda: ran.append(1), client=client, record=RECORD, fail_open=False)
    assert ran == [] and "refund" not in ctx.step.memos


def test_bare_step_needs_run_id(client: Client):
    with pytest.raises(ValueError, match="run_id"):
        recorded_step(FakeStep(), "x", lambda: 1, client=client, record=RECORD)
    out = recorded_step(FakeStep(), "x", lambda: 1, client=client, record=RECORD, run_id="r9")
    assert out == 1


def test_api_key_never_logged(transport: FakeTransport, caplog):
    secret = "spk_live_do_not_log"
    cl = Client(api_key=secret, transport=transport)
    transport.handler = lambda m, p, b: (401, {"error": {"code": "unauthorized", "message": "bad key"}})
    with caplog.at_level(logging.WARNING):
        recorded_step(FakeCtx("r", FakeStep()), "s", lambda: 1, client=cl, record=RECORD)
    assert secret not in caplog.text


# ---- recorded_step_async ----


def test_recorded_step_async_with_async_fn(client: Client, transport: FakeTransport):
    ctx = FakeCtx("run_2", FakeStepAsync())

    async def refund(order_id: str) -> str:
        return f"refunded:{order_id}"

    out = asyncio.run(recorded_step_async(ctx, "refund", refund, "o1", client=client, record=RECORD))
    assert out == "refunded:o1"
    [body] = transport.posted_actions
    assert body["action_id"] == "inngest:run_2:refund"


def test_recorded_step_async_with_sync_fn_and_memo(client: Client, transport: FakeTransport):
    ctx = FakeCtx("run_2", FakeStepAsync(memos={"refund": "memo"}))
    assert (
        asyncio.run(recorded_step_async(ctx, "refund", lambda: "fresh", client=client, record=RECORD))
        == "memo"
    )
    assert transport.calls == []
    ctx2 = FakeCtx("run_3", FakeStepAsync())
    assert (
        asyncio.run(recorded_step_async(ctx2, "refund", lambda: "fresh", client=client, record=RECORD))
        == "fresh"
    )
    assert len(transport.posted_actions) == 1


# ---- wait_for_verdict ----


def test_wait_settles_on_third_poll_with_sleeps_between(client: Client, transport: FakeTransport):
    transport.responses = [
        (200, verification("pending", "UNKNOWN", "UNKNOWN")),
        (200, verification("pending", "UNKNOWN", "UNKNOWN")),
        (200, verification("settled", "PASS", "PASS", action_id="inngest:run_1:refund")),
    ]
    ctx = FakeCtx("run_1", FakeStep())
    v = wait_for_verdict(ctx, "inngest:run_1:refund", client=client, timeout_seconds=60, poll_seconds=10)
    assert v["passed"] is True and v["timed_out"] is False and v["action_id"] == "inngest:run_1:refund"
    assert ctx.step.ran == [f"commitlayer_verify:inngest:run_1:refund:poll:{i}" for i in range(3)]
    assert ctx.step.sleeps == [
        (f"commitlayer_verify:inngest:run_1:refund:sleep:{i}", timedelta(seconds=10)) for i in range(2)
    ]
    assert [p for _, p, _ in transport.calls] == ["/v1/actions/inngest:run_1:refund/wait?timeout=10"] * 3


def test_wait_times_out_after_the_planned_polls(client: Client, transport: FakeTransport):
    transport.handler = lambda m, p, b: (200, verification("pending", "UNKNOWN", "UNKNOWN"))
    ctx = FakeCtx("run_1", FakeStep())
    v = wait_for_verdict(ctx, "a1", client=client, timeout_seconds=25, poll_seconds=10, step_prefix="w")
    assert v["timed_out"] is True and v["settled"] is False and v["passed"] is False
    assert ctx.step.ran == ["w:poll:0", "w:poll:1", "w:poll:2"] and len(ctx.step.sleeps) == 2


def test_wait_replay_uses_memoised_polls(client: Client, transport: FakeTransport):
    settled = dict(
        action_id="a1", contract_id="c", contract_version=1, status="settled", settled=True, outcome="PASS",
        policy="PASS", passed=True, reason_codes={}, reason_labels={}, headline="h", settles_at=None,
        verdict_hash=None, timed_out=False,
    )  # fmt: skip
    pending = {**settled, "status": "pending", "settled": False, "passed": False}
    step = FakeStep(memos={"w:poll:0": pending, "w:poll:1": settled})
    v = wait_for_verdict(FakeCtx("r", step), "a1", client=client, timeout_seconds=100, step_prefix="w")
    assert v["passed"] is True and transport.calls == [] and step.ran == []


def test_wait_fail_verdict_is_settled_and_not_passed(client: Client, transport: FakeTransport):
    transport.responses = [(200, verification("settled", "PASS", "FAIL"))]
    v = wait_for_verdict(FakeCtx("r", FakeStep()), "a1", client=client, timeout_seconds=10)
    assert v["settled"] and not v["passed"] and v["policy"] == "FAIL"
    assert v["reason_codes"]["policy"] == ["limit_exceeded:amount"]


def test_wait_async(client: Client, transport: FakeTransport):
    transport.responses = [
        (200, verification("pending", "UNKNOWN", "UNKNOWN")),
        (200, verification("settled", "PASS", "PASS")),
    ]
    ctx = FakeCtx("r", FakeStepAsync())
    v = asyncio.run(wait_for_verdict_async(ctx, "a1", client=client, timeout_seconds=30, poll_seconds=10))
    assert v["passed"] is True and len(ctx.step.sleeps) == 1 and len(ctx.step.ran) == 2


def test_wait_rejects_bad_poll_interval(client: Client):
    with pytest.raises(ValueError):
        wait_for_verdict(FakeCtx("r", FakeStep()), "a1", client=client, timeout_seconds=1, poll_seconds=0)


# ---- CommitLayerSteps binder ----


def test_binder_applies_fn_id_and_agent_defaults(client: Client, transport: FakeTransport):
    cls = CommitLayerSteps(client, fn_id="order-refund", agent_id="support-agent", poll_seconds=5)
    ctx = FakeCtx("run_9", FakeStep())
    out = cls.run(ctx, "refund", lambda: "ok", **RECORD)
    assert out == "ok"
    [body] = transport.posted_actions
    assert body["workflow_id"] == "order-refund" and body["agent_id"] == "support-agent"

    transport.responses = [(200, verification("settled", "PASS", "PASS"))]
    v = cls.wait_for_verdict(ctx, body["action_id"], timeout_seconds=5)
    assert v["passed"] is True


def test_binder_async(client: Client, transport: FakeTransport):
    cls = CommitLayerSteps(client, fn_id="f", default_timeout_seconds=20, poll_seconds=10)
    ctx = FakeCtx("run_9", FakeStepAsync())
    transport.responses = [(201, {"action": {}}), (200, verification("settled", "FAIL", "FAIL"))]

    async def go():
        out = await cls.run_async(ctx, "refund", lambda: "ok", **RECORD)
        return out, await cls.wait_for_verdict_async(ctx, "inngest:run_9:refund")

    out, v = asyncio.run(go())
    assert out == "ok" and v["settled"] and v["outcome"] == "FAIL"
