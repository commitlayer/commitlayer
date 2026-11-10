"""The shared tool recorder: the body a mapped tool call produces, the fail-open default, and the matching rules."""

from __future__ import annotations

import pytest
from commitlayer_integrations.tools import MappingError, ToolRecorder, build_tool_action, resolve_path

MAPPING = {
    "refund_payment": {
        "contract_id": "refund_workflow",
        "action_type": "refund_order",
        "subject": "arguments.order_id",
        "subject_kind": "order",
        "params": {"amount": "arguments.amount", "currency": "currency"},
        "action_id": "refund:{arguments.order_id}",
    },
    "stripe.create_refund": {"contract_id": "refund_workflow", "subject": "charge"},
}


def test_body_takes_subject_params_and_id_from_the_call():
    body = build_tool_action(
        MAPPING["refund_payment"],
        tool="refund_payment",
        arguments={"order_id": "order_10231", "amount": 180.0, "currency": "USD"},
        call_id="call_7",
        run_id="run_1",
        channel="langgraph",
        agent_id="support-bot",
        requested_at="2026-11-09T10:00:00Z",
    )
    assert body == {
        "action_id": "refund:order_10231",
        "contract_id": "refund_workflow",
        "subject_id": "order_10231",
        "requested_at": "2026-11-09T10:00:00Z",
        "params": {"amount": 180.0, "currency": "USD", "tool": "refund_payment", "tool_call_id": "call_7"},
        "channel": "langgraph",
        "agent_id": "support-bot",
        "workflow_id": "run_1",
        "action_type": "refund_order",
        "subject_kind": "order",
    }


def test_default_action_id_is_stable_per_call_so_a_retry_updates_one_record():
    a = build_tool_action({"contract_id": "c", "subject": "x"}, tool="t", arguments={"x": 1}, call_id="k1")
    b = build_tool_action({"contract_id": "c", "subject": "x"}, tool="t", arguments={"x": 1}, call_id="k1")
    assert a["action_id"] == b["action_id"] == "agent:t:k1"


def test_paths_resolve_into_arguments_and_the_call_itself():
    call = {"tool": "t", "arguments": {"items": [{"id": "i1"}], "n": 2}, "call_id": "k", "run_id": "r"}
    assert resolve_path("arguments.items.0.id", call) == "i1"
    assert resolve_path("n", call) == 2
    assert resolve_path("tool", call) == "t"
    with pytest.raises(MappingError):
        resolve_path("arguments.missing", call)
    with pytest.raises(MappingError):
        build_tool_action({"contract_id": "c", "subject": "x", "bogus": 1}, tool="t", arguments={})


def test_recorder_posts_mapped_calls_and_matches_server_prefixed_names(client, transport):
    rec = ToolRecorder(client, MAPPING, channel="crewai", agent_id="ops-agent")
    out = rec.record("refund_payment", {"order_id": "o1", "amount": 5, "currency": "USD"}, call_id="c1")
    assert out is not None and transport.posted_actions[-1]["action_id"] == "refund:o1"
    assert transport.posted_actions[-1]["channel"] == "crewai"
    assert transport.posted_actions[-1]["agent_id"] == "ops-agent"
    # the bare name matches a mapping written with its server prefix
    assert rec.record("create_refund", {"charge": "ch_1"}, call_id="c2") is not None
    assert transport.posted_actions[-1]["subject_id"] == "ch_1"
    # an unmapped tool is not recorded and not an error
    assert rec.record("search_docs", {"q": "x"}) is None
    assert len(rec.recorded) == 2


def test_recording_is_fail_open_unless_asked_otherwise(client, transport):
    transport.handler = lambda m, p, b: (503, {"error": {"code": "busy", "message": "overloaded"}})
    rec = ToolRecorder(client, MAPPING)
    assert rec.record("refund_payment", {"order_id": "o1", "amount": 5, "currency": "USD"}) is None
    strict = ToolRecorder(client, MAPPING, fail_open=False)
    with pytest.raises(Exception):  # noqa: B017 - the SDK's error type is whatever the client raises
        strict.record("refund_payment", {"order_id": "o1", "amount": 5, "currency": "USD"})
    # a mapping whose path does not fit the call is a MappingError under fail_open=False
    with pytest.raises(MappingError):
        strict.record("refund_payment", {"amount": 5})
