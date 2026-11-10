"""The interceptor and the verifier activity, run inside ``temporalio.testing.ActivityEnvironment``.

No Temporal server: the environment sets the activity context (``activity.info()``, ``heartbeat()``), a fake
next interceptor stands in for the worker, and the SDK client uses the fake transport.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import pytest
from conftest import FakeTransport, verification

temporalio = pytest.importorskip("temporalio", reason="temporalio is not installed (pip install temporalio)")

from commitlayer import Client  # noqa: E402
from commitlayer_temporal import (  # noqa: E402
    RECORD_FAILED,
    CommitLayerInterceptor,
    CommitLayerVerifier,
)
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment  # noqa: E402
from temporalio.worker import ExecuteActivityInput  # noqa: E402

MAPPING = {
    "refund_payment": {
        "contract_id": "refund_workflow",
        "subject": "kwargs.order_id",
        "params": {"amount": "kwargs.amount"},
        "subject_kind": "order",
    },
}


def refund_payment(order_id: str, amount: float) -> dict[str, Any]:
    return {"refund_id": "re_1", "order_id": order_id, "amount": amount}


class FakeNext:
    """The worker's own inbound interceptor: runs the activity function."""

    def __init__(self) -> None:
        self.calls: list[ExecuteActivityInput] = []

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self.calls.append(input)
        return input.fn(*input.args)


def env_for(activity_type: str, *, workflow_id: str = "wf-1", activity_id: str = "5", attempt: int = 1):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        activity_type=activity_type,
        workflow_id=workflow_id,
        activity_id=activity_id,
        attempt=attempt,
    )
    return env


def run_intercepted(interceptor: CommitLayerInterceptor, env: ActivityEnvironment, fn, *args):
    nxt = FakeNext()
    inbound = interceptor.intercept_activity(nxt)  # type: ignore[arg-type]
    inp = ExecuteActivityInput(fn=fn, args=list(args), executor=None, headers={})
    result = asyncio.run(env.run(inbound.execute_activity, inp))
    return result, nxt


# ---- interceptor ----


def test_mapped_activity_is_recorded_before_it_runs(client: Client, transport: FakeTransport):
    order: list[str] = []
    transport.handler = lambda m, p, b: (order.append("recorded"), (201, {"action": b}))[1]

    def fn(order_id: str, amount: float):
        order.append("ran")
        return refund_payment(order_id, amount)

    recorded: list[dict] = []
    icp = CommitLayerInterceptor(client, MAPPING, agent_id="refund-agent", on_recorded=recorded.append)
    result, nxt = run_intercepted(
        icp, env_for("refund_payment", activity_id="9", attempt=2), fn, "order_1", 180.0
    )

    assert order == ["recorded", "ran"]
    assert result == {"refund_id": "re_1", "order_id": "order_1", "amount": 180.0}
    assert len(nxt.calls) == 1 and nxt.calls[0].args == ["order_1", 180.0]
    [body] = transport.posted_actions
    assert body["action_id"] == "temporal:wf-1:9"
    assert body["contract_id"] == "refund_workflow" and body["workflow_id"] == "wf-1"
    assert body["agent_id"] == "refund-agent" and body["channel"] == "temporal"
    assert body["subject_id"] == "order_1" and body["subject_kind"] == "order"
    assert body["params"] == {"amount": 180.0, "temporal_activity_id": "9", "temporal_attempt": 2}
    assert "result" not in body and "refund_id" not in str(body)  # the activity's result never leaves
    assert recorded == [{"action": body}]


def test_unmapped_activity_runs_without_a_request(client: Client, transport: FakeTransport):
    icp = CommitLayerInterceptor(client, MAPPING)
    result, nxt = run_intercepted(icp, env_for("send_email"), lambda to: f"sent:{to}", "a@b")
    assert result == "sent:a@b" and transport.calls == [] and len(nxt.calls) == 1


def test_fail_open_logs_and_runs_activity(client: Client, transport: FakeTransport, caplog):
    transport.handler = lambda m, p, b: (503, {"error": {"code": "unavailable", "message": "down"}})
    icp = CommitLayerInterceptor(client, MAPPING)
    with caplog.at_level(logging.WARNING, logger="commitlayer.temporal"):
        result, nxt = run_intercepted(icp, env_for("refund_payment"), refund_payment, "o1", 1.0)
    assert result["refund_id"] == "re_1" and len(nxt.calls) == 1
    assert any("action not recorded for activity refund_payment" in r.message for r in caplog.records)


def test_fail_open_covers_mapping_errors_too(client: Client, caplog):
    icp = CommitLayerInterceptor(
        client, {"refund_payment": {"contract_id": "c", "subject": "kwargs.missing"}}
    )
    with caplog.at_level(logging.WARNING, logger="commitlayer.temporal"):
        result, _ = run_intercepted(icp, env_for("refund_payment"), refund_payment, "o1", 1.0)
    assert result["refund_id"] == "re_1"
    assert any("kwargs.missing" in r.message for r in caplog.records)


def test_fail_closed_raises_non_retryable_before_activity(client: Client, transport: FakeTransport):
    transport.handler = lambda m, p, b: (503, {"error": {"code": "unavailable", "message": "down"}})
    icp = CommitLayerInterceptor(client, MAPPING, fail_open=False)
    nxt = FakeNext()
    inbound = icp.intercept_activity(nxt)  # type: ignore[arg-type]
    inp = ExecuteActivityInput(fn=refund_payment, args=["o1", 1.0], executor=None, headers={})
    with pytest.raises(ApplicationError) as ei:
        asyncio.run(env_for("refund_payment").run(inbound.execute_activity, inp))
    assert ei.value.non_retryable is True and ei.value.type == RECORD_FAILED
    assert nxt.calls == []  # the activity never ran


def test_transport_errors_never_mention_the_api_key(transport: FakeTransport, caplog):
    secret = "spk_live_do_not_log"
    cl = Client(api_key=secret, transport=transport)
    transport.handler = lambda m, p, b: (401, {"error": {"code": "unauthorized", "message": "bad key"}})
    icp = CommitLayerInterceptor(cl, MAPPING)
    with caplog.at_level(logging.WARNING):
        run_intercepted(icp, env_for("refund_payment"), refund_payment, "o1", 1.0)
    assert secret not in caplog.text


def test_mapping_accepts_dataclass_and_positional_paths(client: Client, transport: FakeTransport):
    from commitlayer_temporal import ActivityMapping

    icp = CommitLayerInterceptor(
        client,
        {
            "refund_payment": ActivityMapping(
                "c", "args.0", params={"amount": "args.1"}, action_id="r:{subject_id}"
            )
        },
        workflow_id="billing",
    )
    run_intercepted(icp, env_for("refund_payment"), refund_payment, "o7", 3.0)
    [body] = transport.posted_actions
    assert (
        body["action_id"] == "r:o7" and body["workflow_id"] == "billing" and body["params"]["amount"] == 3.0
    )


# ---- verifier activity ----


def test_verifier_waits_until_settled_and_heartbeats(client: Client, transport: FakeTransport):
    transport.responses = [
        (200, verification("pending", "UNKNOWN", "UNKNOWN")),
        (200, verification("pending", "UNKNOWN", "UNKNOWN")),
        (200, verification("settled", "PASS", "PASS", action_id="temporal:wf-1:5")),
    ]
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    verifier = CommitLayerVerifier(client, sleep=sleep, clock=lambda: clock[0], poll_seconds=2)
    env = env_for("commitlayer_verify")
    beats: list[Any] = []
    env.on_heartbeat = lambda *d: beats.append(d)
    verdict = env.run(verifier.commitlayer_verify, "temporal:wf-1:5", 60)

    assert verdict["passed"] is True and verdict["status"] == "settled" and verdict["timed_out"] is False
    assert verdict["action_id"] == "temporal:wf-1:5" and verdict["policy"] == "PASS"
    # Each poll goes through the completion gate, held for the poll interval.
    assert [p for _, p, _ in transport.calls] == ["/v1/actions/temporal:wf-1:5/wait?timeout=2"] * 3
    assert sleeps == [2, 2] and len(beats) == 3
    assert beats[-1] == ({"status": "settled", "outcome": "PASS", "policy": "PASS"},)


def test_verifier_times_out_while_pending(client: Client, transport: FakeTransport):
    transport.handler = lambda m, p, b: (200, verification("pending", "UNKNOWN", "UNKNOWN"))
    clock = [0.0]

    def sleep(s: float) -> None:
        clock[0] += s

    verifier = CommitLayerVerifier(client, sleep=sleep, clock=lambda: clock[0], poll_seconds=5)
    verdict = env_for("commitlayer_verify").run(verifier.commitlayer_verify, "a1", 11)
    assert verdict["timed_out"] is True and verdict["settled"] is False and verdict["passed"] is False
    assert len(transport.calls) == 4  # t=0, 5, 10 and a last look at the deadline (t=11)


def test_verifier_uses_defaults_when_workflow_passes_only_the_ref(client: Client, transport: FakeTransport):
    transport.responses = [(200, verification("settled", "PASS", "FAIL"))]
    verifier = CommitLayerVerifier(client, default_timeout_seconds=1, sleep=lambda s: None)
    verdict = env_for("commitlayer_verify").run(verifier.commitlayer_verify, "a1")
    assert verdict["settled"] is True and verdict["passed"] is False
    assert verdict["reason_codes"]["policy"] == ["limit_exceeded:amount"]


def test_verifier_stops_as_cancelled(client: Client, transport: FakeTransport):
    from temporalio.exceptions import CancelledError

    env = env_for("commitlayer_verify")

    def fetch(m, p, b):
        env.cancel()  # like a worker, the environment raises CancelledError in the sync activity's thread
        return 200, verification("pending", "UNKNOWN", "UNKNOWN")

    transport.handler = fetch
    verifier = CommitLayerVerifier(client, sleep=lambda s: None)
    with pytest.raises(CancelledError):
        env.run(verifier.commitlayer_verify, "a1", 600)
    assert len(transport.calls) == 1


def test_verifier_checks_cancellation_between_polls(client: Client, transport: FakeTransport, monkeypatch):
    """The ``is_cancelled`` path (a worker running the activity with ``no_thread_cancel_exception``)."""
    from temporalio import activity
    from temporalio.exceptions import CancelledError

    cancelled = [False]
    monkeypatch.setattr(activity, "is_cancelled", lambda: cancelled[0])

    def fetch(m, p, b):
        cancelled[0] = True  # the request lands while the poll is in flight
        return 200, verification("pending", "UNKNOWN", "UNKNOWN")

    transport.handler = fetch
    verifier = CommitLayerVerifier(client, sleep=lambda s: None)
    with pytest.raises(CancelledError):
        env_for("commitlayer_verify").run(verifier.commitlayer_verify, "a1", 600)
    assert len(transport.calls) == 1


def test_verifier_propagates_api_errors(client: Client, transport: FakeTransport):
    from commitlayer import CommitLayerError

    transport.handler = lambda m, p, b: (404, {"error": {"code": "not_found", "message": "no such action"}})
    verifier = CommitLayerVerifier(client, sleep=lambda s: None)
    with pytest.raises(CommitLayerError):
        env_for("commitlayer_verify").run(verifier.commitlayer_verify, "nope", 1)


def test_activity_is_registered_under_its_public_name():
    from commitlayer_temporal import ACTIVITY_NAME
    from temporalio import activity

    assert activity._Definition.from_callable(CommitLayerVerifier.commitlayer_verify).name == ACTIVITY_NAME
    assert ACTIVITY_NAME == "commitlayer_verify"
