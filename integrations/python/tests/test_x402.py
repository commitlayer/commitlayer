"""The x402 observer: a 402 quote, a paid retry and a settled response become one action and two facts; a pending
settlement is pushed as pending; nothing sensitive is kept; the facts travel with the evidence client when there
is one."""

from __future__ import annotations

import base64
import json

import pytest

httpx = pytest.importorskip("httpx", reason="httpx is not installed (pip install httpx)")

from commitlayer_x402 import X402Observer, amount_from_atomic, decode_header, settlement_status

from commitlayer import Client

from conftest import FakeTransport

NONCE = "0x6e0f6b2d4c7a4a7e9d1b3f5c8a2e4b6d7f9a1c3e5b7d9f1a3c5e7b9d1f3a5c7e"
URL = "https://api.example-data.com/v1/quote"
ACCEPT = {
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "50000",
    "asset": "0x833589fCB6eE2CBd66d4A3DBe0B0F7F9b9B0b4C1",
    "payTo": "0x2222222222222222222222222222222222222222",
    "maxTimeoutSeconds": 60,
}


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def required() -> str:
    return b64({"x402Version": 2, "resource": {"url": URL, "description": "quote"}, "accepts": [ACCEPT]})


def signature() -> str:
    return b64(
        {
            "x402Version": 2,
            "accepted": ACCEPT,
            "payload": {
                "signature": "0xdeadbeef",
                "authorization": {
                    "from": "0x1111111111111111111111111111111111111111",
                    "to": ACCEPT["payTo"],
                    "value": "50000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": NONCE,
                },
            },
        }
    )


def response_header(success=True, reason=None) -> str:
    body = {"success": success, "transaction": "0x3f2a" if success or reason else "", "network": "eip155:8453", "payer": "0x1111111111111111111111111111111111111111"}
    if reason:
        body["errorReason"] = reason
    return b64(body)


def _server(final_status=200, settle=True, reason=None):
    """The paid resource: 402 without a signature, the resource with one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "payment-signature" not in request.headers:
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": required()}, json={})
        headers = {"PAYMENT-RESPONSE": response_header(settle, reason)} if settle is not None else {}
        return httpx.Response(final_status, headers=headers, content=b"x" * 1200)

    return httpx.MockTransport(handler)


def _buy(client: httpx.Client) -> httpx.Response:
    """What an x402 client library does: see the 402, pay, retry."""
    r = client.get(URL)
    assert r.status_code == 402
    return client.get(URL, headers={"PAYMENT-SIGNATURE": signature()})


def test_quote_payment_and_settlement_become_one_action_and_two_facts():
    recorder_t, evidence_t = FakeTransport(), FakeTransport()
    obs = X402Observer(Client(transport=recorder_t), evidence=Client(transport=evidence_t), kind="api_call", contract_version=3, agent_id="bot")
    with httpx.Client(transport=_server(), event_hooks=obs.httpx_hooks()) as c:
        r = _buy(c)
    assert r.status_code == 200
    # the action, from the quote and the nonce, through the recorder key
    [body] = recorder_t.posted_actions
    assert body["contract_id"] == "x402_purchase_api_call" and body["contract_version"] == 3
    assert body["subject_id"] == NONCE and body["subject_kind"] == "payment" and body["action_id"] == f"purchase:{NONCE}"
    assert body["params"]["amount"] == 0.05 and body["params"]["amount_atomic"] == "50000"
    assert body["params"]["network"] == "eip155:8453" and body["params"]["pay_to"] == ACCEPT["payTo"]
    assert body["params"]["resource"] == URL and body["params"]["provider"] == "api.example-data.com"
    assert body["params"]["payer"] == "0x1111111111111111111111111111111111111111"
    assert body["channel"] == "x402" and body["agent_id"] == "bot"
    assert "signature" not in json.dumps(body) and "deadbeef" not in json.dumps(body)
    # the facts, through the evidence key, never the recorder's
    assert not [c for c in recorder_t.calls if c[1] == "/v1/facts"]
    [(_, path, facts)] = [c for c in evidence_t.calls if c[1] == "/v1/facts"]
    by = {f["fact_type"]: f for f in facts["facts"]}
    s = by["settlement"]
    assert s["source"] == "x402" and s["subject_id"] == NONCE and s["attributes"]["status"] == "settled"
    assert s["attributes"]["transaction"] == "0x3f2a" and s["attributes"]["amount"] == 0.05
    assert s["raw_ref"] == f"x402:settlement:{NONCE}:settled"
    d = by["delivered"]
    assert d["source"] == "delivery" and d["attributes"] == {"kind": "api_call", "http_status": 200, "resource": URL, "bytes": 1200}
    assert obs.last.settlement == "settled" and obs.last.delivered is True


def test_pending_and_failed_settlements_and_an_error_after_payment():
    t = FakeTransport()
    obs = X402Observer(Client(transport=t), kind="dataset", decimals=6)
    with httpx.Client(transport=_server(settle=False, reason="settlement_pending"), event_hooks=obs.httpx_hooks()) as c:
        _buy(c)
    facts = [c for c in t.calls if c[1] == "/v1/facts"][-1][2]["facts"]
    s = next(f for f in facts if f["fact_type"] == "settlement")
    assert s["attributes"]["status"] == "pending" and s["attributes"]["error_reason"] == "settlement_pending"
    assert s["raw_ref"].endswith(":pending")
    # paid, then a 500: delivery_failed, and a failed settlement
    obs2 = X402Observer(Client(transport=t), kind="dataset")
    with httpx.Client(transport=_server(final_status=500, settle=False, reason="insufficient_funds"), event_hooks=obs2.httpx_hooks()) as c:
        _buy(c)
    facts = [c for c in t.calls if c[1] == "/v1/facts"][-1][2]["facts"]
    kinds = {f["fact_type"]: f for f in facts}
    assert kinds["settlement"]["attributes"]["status"] == "failed"
    assert kinds["delivery_failed"]["attributes"]["http_status"] == 500 and kinds["delivery_failed"]["attributes"]["kind"] == "dataset"
    # without an evidence client the facts go through the recorder client (the service classes them agent_self_report)
    assert any(c[1] == "/v1/facts" for c in t.calls)


def test_fail_open_and_unpaid_requests():
    def broken(method, path, body):
        return 503, {"error": {"code": "unavailable", "message": "down"}}

    t = FakeTransport(handler=broken)
    obs = X402Observer(Client(transport=t), kind="mcp_tool_call")
    with httpx.Client(transport=_server(), event_hooks=obs.httpx_hooks()) as c:
        r = _buy(c)  # the purchase proceeds although CommitLayer is down
    assert r.status_code == 200 and obs.last.recorded is None
    # a request without PAYMENT-SIGNATURE is not a purchase
    t2 = FakeTransport()
    obs2 = X402Observer(Client(transport=t2), kind="api_call")
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok")), event_hooks=obs2.httpx_hooks()) as c:
        c.get("https://free.example.com/")
    assert t2.calls == [] and obs2.purchases == []


def test_helpers():
    assert decode_header(None) is None and decode_header("!!") is None
    assert decode_header(base64.b64encode(b'{"a": 1}').decode()) == {"a": 1}
    assert amount_from_atomic("50000", 6) == 0.05 and amount_from_atomic("7", None) == 7.0 and amount_from_atomic("x", 6) is None
    assert settlement_status({"success": True}) == "settled"
    assert settlement_status({"success": False, "errorReason": "settlement_pending", "transaction": "0x1"}) == "pending"
    assert settlement_status({"success": False, "errorReason": "insufficient_funds"}) == "failed"
    try:
        X402Observer(Client(transport=FakeTransport()), kind="pizza")
    except ValueError as e:
        assert "kind" in str(e)
    else:
        raise AssertionError("an unknown kind must be refused")
