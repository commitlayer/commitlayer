"""The pure part of the Temporal integration: paths, templates and the recorded body. No temporalio needed."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from commitlayer_temporal.mapping import (
    ActivityMapping,
    MappingError,
    TemporalRef,
    bind_kwargs,
    build_action_body,
    resolve_path,
)

REF = TemporalRef(
    workflow_id="wf-42", activity_id="7", attempt=3, activity_type="refund_payment", workflow_run_id="run-1"
)


@dataclass
class RefundRequest:
    order_id: str
    amount: float
    reason: str = "customer_cancelled"


def refund_payment(order_id: str, amount: float, reason: str = "other") -> dict:
    return {"ok": True}


# ---- paths ----


def test_resolve_args_index_and_key():
    assert resolve_path("args.0.order_id", [{"order_id": "o1"}], {}) == "o1"


def test_resolve_args_attribute_on_dataclass():
    assert resolve_path("args.0.amount", [RefundRequest("o1", 180.0)], {}) == 180.0


def test_resolve_kwargs_and_bare_name():
    kw = {"order": {"id": "o9"}, "amount": 5}
    assert resolve_path("kwargs.order.id", [], kw) == "o9"
    assert resolve_path("amount", [], kw) == 5


def test_resolve_errors_name_the_path():
    with pytest.raises(MappingError, match="args.3"):
        resolve_path("args.3", ["a"], {})
    with pytest.raises(MappingError, match="no key 'x'"):
        resolve_path("args.0.x", [{"y": 1}], {})
    with pytest.raises(MappingError, match="no attribute 'nope'"):
        resolve_path("args.0.nope", [RefundRequest("o1", 1)], {})
    with pytest.raises(MappingError):
        resolve_path("", [], {})


def test_bind_kwargs_from_signature():
    assert bind_kwargs(refund_payment, ["o1", 180.0]) == {
        "order_id": "o1",
        "amount": 180.0,
        "reason": "other",
    }
    assert bind_kwargs(refund_payment, ["o1", 1, "x", "too many"]) == {}


def test_bind_kwargs_skips_self_on_bound_methods():
    class Acts:
        def refund(self, order_id: str, amount: float) -> None: ...

    assert bind_kwargs(Acts().refund, ["o1", 2.0]) == {"order_id": "o1", "amount": 2.0}


# ---- body ----


def test_body_is_exactly_what_post_v1_actions_accepts():
    mapping = {
        "contract_id": "refund_workflow",
        "subject": "kwargs.order_id",
        "params": {"amount": "kwargs.amount", "reason": "kwargs.reason"},
        "action_type": "refund_order",
        "subject_kind": "order",
        "contract_version": 2,
    }
    body = build_action_body(
        mapping,
        ref=REF,
        args=["order_10231", 180.0],
        kwargs=bind_kwargs(refund_payment, ["order_10231", 180.0]),
        agent_id="refund-agent",
        requested_at="2026-10-15T10:42:00Z",
    )
    assert body == {
        "action_id": "temporal:wf-42:7",
        "contract_id": "refund_workflow",
        "workflow_id": "wf-42",
        "subject_id": "order_10231",
        "requested_at": "2026-10-15T10:42:00Z",
        "params": {"amount": 180.0, "reason": "other", "temporal_activity_id": "7", "temporal_attempt": 3},
        "channel": "temporal",
        "agent_id": "refund-agent",
        "action_type": "refund_order",
        "subject_kind": "order",
        "contract_version": 2,
    }
    assert set(body) <= {
        "action_id", "contract_id", "contract_version", "workflow_id", "agent_id", "action_type",
        "subject_id", "subject_kind", "requested_at", "params", "channel", "compensates",
    }  # fmt: skip


def test_body_minimal_omits_unset_optionals_and_stamps_requested_at():
    body = build_action_body(ActivityMapping("c", "args.0"), ref=REF, args=["subj"])
    assert set(body) == {
        "action_id",
        "contract_id",
        "workflow_id",
        "subject_id",
        "requested_at",
        "params",
        "channel",
    }
    assert body["requested_at"].endswith("Z") and len(body["requested_at"]) == 20
    assert body["params"] == {"temporal_activity_id": "7", "temporal_attempt": 3}


def test_default_action_id_is_stable_across_attempts():
    a = build_action_body(ActivityMapping("c", "args.0"), ref=REF, args=["s"])
    b = build_action_body(ActivityMapping("c", "args.0"), ref=TemporalRef("wf-42", "7", 4, "x"), args=["s"])
    assert a["action_id"] == b["action_id"] == "temporal:wf-42:7"
    assert b["params"]["temporal_attempt"] == 4


def test_action_id_template_uses_params_subject_and_temporal_fields():
    m = ActivityMapping(
        "c",
        "args.0.order_id",
        params={"amount": "args.0.amount"},
        action_id="refund:{subject_id}:{amount}:{activity}",
    )
    body = build_action_body(m, ref=REF, args=[RefundRequest("o1", 180.0)])
    assert body["action_id"] == "refund:o1:180.0:refund_payment"
    m2 = ActivityMapping("c", "args.0", action_id="{workflow_id}/{workflow_run_id}/{activity_id}/{attempt}")
    assert build_action_body(m2, ref=REF, args=["s"])["action_id"] == "wf-42/run-1/7/3"


def test_action_id_template_unknown_placeholder_is_an_error():
    with pytest.raises(MappingError, match="unknown placeholder"):
        build_action_body(ActivityMapping("c", "args.0", action_id="{nope}"), ref=REF, args=["s"])


def test_action_id_too_long_is_an_error():
    with pytest.raises(MappingError, match="1-120"):
        build_action_body(ActivityMapping("c", "args.0", action_id="x" * 121), ref=REF, args=["s"])


def test_workflow_id_override_and_missing_workflow():
    body = build_action_body(ActivityMapping("c", "args.0"), ref=REF, args=["s"], workflow_id="billing")
    assert body["workflow_id"] == "billing"
    ref = TemporalRef(workflow_id=None, activity_id="1", attempt=1, activity_type="a")
    body = build_action_body(ActivityMapping("c", "args.0"), ref=ref, args=["s"])
    assert "workflow_id" not in body and body["action_id"] == "temporal::1"


def test_compensates_is_a_path():
    m = ActivityMapping("reversal_workflow", "kwargs.order_id", compensates="kwargs.failed_action_id")
    body = build_action_body(m, ref=REF, kwargs={"order_id": "o1", "failed_action_id": "temporal:wf-42:3"})
    assert body["compensates"] == "temporal:wf-42:3"


def test_subject_must_resolve_to_something():
    with pytest.raises(MappingError, match="resolved to nothing"):
        build_action_body(ActivityMapping("c", "kwargs.order_id"), ref=REF, kwargs={"order_id": None})
    with pytest.raises(MappingError):
        build_action_body(ActivityMapping("c", "kwargs.order_id"), ref=REF, kwargs={})


def test_missing_param_path_is_an_error_not_a_silent_zero():
    m = ActivityMapping("c", "args.0", params={"amount": "kwargs.amount"})
    with pytest.raises(MappingError, match="kwargs.amount"):
        build_action_body(m, ref=REF, args=["s"], kwargs={})


def test_mapping_rejects_unknown_keys():
    with pytest.raises(MappingError, match="unknown mapping keys"):
        ActivityMapping.coerce({"contract_id": "c", "subject": "args.0", "result": "args.1"})


def test_ref_from_info_duck_types():
    info = SimpleNamespace(
        workflow_id="w", activity_id="a", attempt=2, activity_type="t", workflow_run_id="r"
    )
    assert TemporalRef.from_info(info) == TemporalRef("w", "a", 2, "t", "r")
