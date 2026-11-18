"""Live contract check, Python: each package's core calls against a running CommitLayer API.

Skipped unless COMMITLAYER_LIVE=1, with COMMITLAYER_API_URL and COMMITLAYER_API_KEY naming a disposable workspace
(scripts/boot_backend.py starts one from a backend checkout and writes the three). The unit tests use fake
transports; this suite is what notices that the real API answers differently: a field renamed in an answer, a
parameter the code now requires, an error envelope that moved. It writes to the workspace it is given, so never
point it at a real one.

    set -a; . build/live.env; set +a
    python -m pytest -q tests/live

Fast and deterministic: no sleeps, every wait bounded to a second or two, ids unique per run.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in ("sdk/python", "recorder/python", "integrations/python"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))

pytestmark = pytest.mark.skipif(
    os.environ.get("COMMITLAYER_LIVE") != "1", reason="live API check: set COMMITLAYER_LIVE=1"
)

CONTRACT = os.environ.get("COMMITLAYER_LIVE_CONTRACT", "refund_workflow")
RUN = uuid.uuid4().hex[:10]
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def client():
    from commitlayer import Client

    return Client(max_retries=0, timeout=30)


@pytest.fixture(scope="module")
def recorded(client):
    """One action recorded with every optional field the SDK can send, and one fact pushed for it."""
    action_id = f"act_live_py_{RUN}"
    subject = f"order_live_py_{RUN}"
    rec = client.record_action(
        CONTRACT,
        subject,
        action_id=action_id,
        params={"amount": 42.5, "currency": "USD"},
        action_type="refund_order",
        subject_kind="order",
        workflow_id=f"wf_live_{RUN}",
        agent_id="live-smoke-agent",
        channel="api",
    )
    facts = client.push_facts(
        [
            {
                "source": "stripe",
                "subject_id": subject,
                "subject_kind": "order",
                "fact_type": "refund",
                "occurred_at": _now(),
                "attributes": {"amount": 42.5, "currency": "USD"},
                "raw_ref": f"live:{RUN}",
            }
        ],
        watermarks={"stripe": _now()},
    )
    return {"action_id": action_id, "subject": subject, "record": rec, "facts": facts}


# ---------------- sdk/python ----------------


def test_record_action_answers_action_verification_and_allowance(recorded):
    rec = recorded["record"]
    assert {"action", "verification", "allowance"} <= set(rec)
    assert rec["action"]["action_id"] == recorded["action_id"]
    assert rec["action"]["contract_id"] == CONTRACT and rec["action"]["subject_id"] == recorded["subject"]


def test_push_facts_accepts_the_fact(recorded):
    assert recorded["facts"]["accepted_count"] == 1 and recorded["facts"]["rejected_count"] == 0


def test_get_action_is_a_verification_the_verdict_readers_understand(client, recorded):
    from commitlayer_integrations.verdict import summarize_verification

    v = client.get_action(recorded["action_id"])
    assert v["action"]["action_id"] == recorded["action_id"]
    assert {"lenses", "status", "summary", "settles_at"} <= set(v)
    for lens in ("outcome", "policy"):
        assert v["lenses"][lens]["verdict"] in VERDICTS
        assert isinstance(v["lenses"][lens]["reason_codes"], list)
        assert isinstance(v["lenses"][lens]["reason_labels"], list)
    assert isinstance(v["summary"]["headline"], str)
    s = summarize_verification(v)
    assert s["action_id"] == recorded["action_id"] and s["contract_id"] == CONTRACT
    assert s["status"] in ("pending", "settled") and s["outcome"] in VERDICTS and s["policy"] in VERDICTS
    assert isinstance(s["contract_version"], int) and s["verdict_hash"]


def test_wait_for_settlement_returns_within_its_budget(client, recorded):
    v = client.wait_for_settlement(recorded["action_id"], timeout=1)
    assert isinstance(v["timed_out"], bool) and v["status"] in ("pending", "settled")
    assert v["action"]["action_id"] == recorded["action_id"]


def test_inline_dry_run_verify(client):
    v = client.verify(
        action={
            "action_id": f"act_live_dry_{RUN}",
            "contract_id": CONTRACT,
            "subject_id": f"order_dry_{RUN}",
            "subject_kind": "order",
            "requested_at": _now(),
            "params": {"amount": 1.0, "currency": "USD"},
        },
        facts=[
            {
                "source": "stripe",
                "subject_id": f"order_dry_{RUN}",
                "subject_kind": "order",
                "fact_type": "refund",
                "occurred_at": _now(),
                "attributes": {"amount": 1.0},
            }
        ],
        dry_run=True,
    )
    assert v["lenses"]["outcome"]["verdict"] in VERDICTS and v["dry_run"] is True


def test_verify_by_id(client, recorded):
    v = client.verify(recorded["action_id"])
    assert v["action"]["action_id"] == recorded["action_id"] and v["lenses"]["policy"]["verdict"] in VERDICTS


def test_contract_draft_validate_and_save(client, recorded):
    d = client.draft_contract("refund_order", lookback_days=1)
    assert {"yaml", "contract", "errors", "warnings", "stats"} <= set(d)
    v = client.validate_contract(d["yaml"])
    # An invalid draft (no history to draft from) answers {valid: false, errors}; a valid one adds warnings.
    assert isinstance(v["valid"], bool) and isinstance(v["errors"], list)
    if v["valid"]:
        assert isinstance(v["warnings"], list)
    # Saving needs a contract the API accepts; a refusal must still come back as the error envelope.
    from commitlayer import CommitLayerError

    try:
        saved = client.save_contract(d["yaml"])
        assert isinstance(saved, dict)
    except CommitLayerError as e:
        assert 400 <= e.status < 500 and e.code not in ("error", "http_error")


def test_packs(client):
    items = client.packs.list()["items"]
    assert items, "the pack library is seeded at start"
    pack = items[0]["id"]
    got = client.packs.get(pack)
    assert got["id"] == pack and "contracts" in got and "recorder_mapping" in got
    conf = client.packs.configure(pack)
    assert conf["pack_id"] == pack and "contracts" in conf
    dry = client.packs.dry_run(pack)
    assert dry["pack_id"] == pack and {"total", "ok"} <= set(dry["summary"])
    cov = client.packs.coverage(pack)
    assert cov["pack_id"] == pack and "sources" in cov
    assert "tools" in client.packs.recorder_mapping(pack)


def test_install_without_confirm_is_refused_with_the_error_envelope(client):
    from commitlayer import CommitLayerError

    pack = client.packs.list()["items"][0]["id"]
    with pytest.raises(CommitLayerError) as e:
        client.packs.install(pack, confirm=False)
    assert 400 <= e.value.status < 500 and e.value.code not in ("error", "http_error") and e.value.message


def test_an_unknown_action_is_a_404_with_the_error_envelope(client):
    from commitlayer import CommitLayerError

    with pytest.raises(CommitLayerError) as e:
        client.get_action(f"act_missing_{RUN}")
    assert e.value.status == 404 and e.value.code not in ("error", "http_error") and e.value.message


def test_a_bad_key_is_refused_with_the_error_envelope():
    from commitlayer import Client, CommitLayerError

    with pytest.raises(CommitLayerError) as e:
        Client(api_key="spk_not_a_real_key_for_the_live_check", max_retries=0).get_action("x")
    assert e.value.status in (401, 403) and e.value.code not in ("error", "http_error")


# ---------------- recorder/python ----------------


def test_recorder_records_a_mapped_tool_call():
    from commitlayer import Client
    from commitlayer_mcp_recorder import Recorder, load_mapping

    mapping = load_mapping(
        {
            "agent_id": "live-recorder",
            "tools": {
                "refund_payment": {
                    "contract_id": CONTRACT,
                    "subject": "arguments.order_id",
                    "subject_kind": "order",
                    "action_type": "refund_order",
                    "params": {"amount": "arguments.amount"},
                    "action_id": "mcp-live:{arguments.order_id}",
                }
            },
        }
    )
    rec = Recorder(
        mapping, api_key=os.environ["COMMITLAYER_API_KEY"], api_url=os.environ["COMMITLAYER_API_URL"], fail_open=False
    )
    order = f"order_rec_py_{RUN}"
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "refund_payment", "arguments": {"order_id": order, "amount": 5}},
    }
    decision = rec.before_forward(msg)
    assert decision.forward and decision.action_id == f"mcp-live:{order}" and decision.error is None
    v = Client(max_retries=0).get_action(decision.action_id)
    assert v["action"]["subject_id"] == order and v["action"]["channel"] == "mcp"
    assert v["action"]["params"]["mcp_tool"] == "refund_payment"


# ---------------- integrations/python ----------------


def test_integrations_record_and_poll_through_the_sdk(client):
    from commitlayer_integrations.verdict import fetch_verification, poll_until_settled
    from commitlayer_temporal.mapping import ActivityMapping, TemporalRef, build_action_body

    mapping = ActivityMapping(
        CONTRACT,
        "args.0.order_id",
        params={"amount": "args.0.amount"},
        subject_kind="order",
        action_type="refund_order",
    )
    ref = TemporalRef(f"wf_tmp_{RUN}", "1", 1, "refund", workflow_run_id="run1")
    body = build_action_body(mapping, ref=ref, args=[{"order_id": f"order_tmp_{RUN}", "amount": 3}])
    rec = client._call("POST", "/v1/actions", body)
    assert rec["action"]["action_id"] == body["action_id"] and rec["action"]["channel"] == body["channel"]
    held = fetch_verification(client, body["action_id"], hold_seconds=1)
    assert held["action"]["action_id"] == body["action_id"]
    verdict = poll_until_settled(lambda: client.get_action(body["action_id"]), timeout_seconds=0, poll_seconds=0)
    assert verdict["action_id"] == body["action_id"] and verdict["outcome"] in VERDICTS
