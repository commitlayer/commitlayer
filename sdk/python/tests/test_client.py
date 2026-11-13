"""The Python SDK over a fake transport: the bodies it sends, the version it reports, the pack helpers, and
what it sends again when the API answers 429 or 503 with ``Retry-After``. The last section runs the built-in
``urllib`` transport against an HTTP server on a loopback port."""

from __future__ import annotations

import copy
import email.message
import http.client
import json
import logging
import pickle
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import commitlayer  # noqa: E402
from commitlayer import Client, CommitLayerError, __version__  # noqa: E402


class FakeTransport:
    def __init__(self, handler=None):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.handler = handler

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body))
        if self.handler:
            return self.handler(method, path, body)
        return 200, {"ok": True, "path": path}


def test_version_constant_matches_the_package_version():
    # Read with a pattern, not tomllib: the package supports Python 3.10, which has no tomllib.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'(?m)^version = "([^"]+)"$', text)
    assert declared is not None and __version__ == declared.group(1)


def test_record_action_body_and_errors():
    t = FakeTransport()
    c = Client(transport=t, agent_id="bot")
    c.record_action("refund_workflow", "ord_1", action_id="refund:ord_1", params={"amount": 40}, contract_version=2)
    method, path, body = t.calls[0]
    assert (method, path) == ("POST", "/v1/actions")
    assert body["action_id"] == "refund:ord_1" and body["subject_id"] == "ord_1" and body["agent_id"] == "bot"
    assert body["contract_version"] == 2 and body["params"] == {"amount": 40} and body["requested_at"].endswith("Z")

    def refuse(method, path, body):
        return 402, {"error": {"code": "plan_required", "message": "Builder plan needed", "details": {"upgrade_to": "builder"}}}

    with pytest.raises(CommitLayerError) as e:
        Client(transport=FakeTransport(refuse)).packs.install("stripe_customer_operations", confirm=True)
    assert e.value.status == 402 and e.value.code == "plan_required" and e.value.details == {"upgrade_to": "builder"}


def test_pack_helpers_call_the_pack_routes():
    t = FakeTransport()
    c = Client(transport=t)
    c.packs.list()
    c.packs.get("x402_service_delivery")
    c.packs.configure("x402_service_delivery", {"purchase_api_call": {"approved_limit": 2}})
    c.packs.dry_run("x402_service_delivery", {"purchase_api_call": {"approved_limit": 2}}, workflow="purchase_api_call")
    c.packs.install("x402_service_delivery", {}, confirm=True)
    c.packs.coverage("x402_service_delivery")
    c.packs.recorder_mapping("x402_service_delivery")
    assert [(m, p) for m, p, _ in t.calls] == [
        ("GET", "/v1/packs"),
        ("GET", "/v1/packs/x402_service_delivery"),
        ("POST", "/v1/packs/x402_service_delivery/configure"),
        ("POST", "/v1/packs/x402_service_delivery/dry-run"),
        ("POST", "/v1/packs/x402_service_delivery/install"),
        ("GET", "/v1/packs/x402_service_delivery/coverage"),
        ("GET", "/v1/packs/x402_service_delivery/recorder-mapping"),
    ]
    assert t.calls[2][2] == {"configuration": {"purchase_api_call": {"approved_limit": 2}}}
    assert t.calls[3][2] == {"configuration": {"purchase_api_call": {"approved_limit": 2}}, "workflow": "purchase_api_call"}
    assert t.calls[4][2] == {"configuration": {}, "confirm": True}
    # install without confirm sends confirm: false and lets the service refuse it: the SDK never activates anything
    c.packs.install("x402_service_delivery")
    assert t.calls[-1][2] == {"configuration": {}, "confirm": False}


# ---------------- retries: 429 and 503 with Retry-After ----------------

WORKSPACE_BUSY = {
    "error": {
        "code": "workspace_busy",
        "message": "This workspace already has 7 writes in progress; the request was not applied.",
        "details": {"in_progress": 7},
    }
}
BUSY = {"error": {"code": "busy", "message": "The service is busy; the request was not applied."}}
RECORDED = {"action": {"action_id": "a", "status": "pending"}, "verification": {}, "allowance": None}
REFUSALS = {429: WORKSPACE_BUSY, 503: BUSY}


class Script:
    """A transport that answers from a list (the last answer repeats) and keeps every request as the bytes the
    body would be on the wire at that moment, so a body changed between attempts cannot hide."""

    def __init__(self, *answers: tuple):
        self.answers = list(answers)
        self.sent: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> tuple:
        self.sent.append((method, path, None if body is None else json.dumps(body).encode()))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def body(self, i: int) -> dict[str, Any]:
        raw = self.sent[i][2]
        assert raw is not None
        return json.loads(raw)


def scripted(*answers: tuple, **options: Any) -> tuple[Client, Script, list[float]]:
    """A client over a ``Script`` whose waits are collected instead of slept and whose jitter is zero."""
    transport, waits = Script(*answers), []
    options.setdefault("jitter", lambda: 0.0)
    return Client(transport=transport, sleep=waits.append, **options), transport, waits


@pytest.mark.parametrize("status", [429, 503])
def test_one_refusal_then_success_waits_what_the_server_stated_and_sends_the_same_body(status):
    c, t, waits = scripted((status, REFUSALS[status], {"Retry-After": "1"}), (201, RECORDED))
    out = c.record_action("refund_workflow", "ord_1", params={"amount": 40})  # the client makes the action_id
    assert out == RECORDED
    assert waits == [1.0]
    assert len(t.sent) == 2 and t.sent[0] == t.sent[1]  # method, path and body, byte for byte
    assert re.fullmatch(r"act_[0-9a-f]{32}", t.body(0)["action_id"])

    c, t, waits = scripted((status, REFUSALS[status], {"Retry-After": "1"}), (201, RECORDED))
    c.record_action("refund_workflow", "ord_1", action_id="refund:ord_1")
    assert waits == [1.0] and t.sent[0] == t.sent[1] and t.body(1)["action_id"] == "refund:ord_1"


def test_the_defaults_are_three_retries_and_a_ceiling_of_ten_seconds():
    c = Client(transport=FakeTransport())
    assert (c.max_retries, c.max_retry_wait) == (3, 10.0)


def test_refusals_past_the_limit_raise_the_last_answer_with_its_wait_and_the_attempts():
    last = {"error": {"code": "busy", "message": "still busy", "details": {"n": 4}}}
    c, t, waits = scripted(
        (429, WORKSPACE_BUSY, {"Retry-After": "1"}),
        (503, BUSY, {"Retry-After": "2"}),
        (429, WORKSPACE_BUSY, {"Retry-After": "1"}),
        (503, last, {"Retry-After": "3"}),
        (201, RECORDED),  # never reached: three retries is the default
    )
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1")
    err = e.value
    assert (err.status, err.code, err.message, err.details) == (503, "busy", "still busy", {"n": 4})
    assert str(err) == "busy (503): still busy"  # the message format is what it was before retries existed
    assert err.retry_after == 3.0 and err.attempts == 4
    assert waits == [1.0, 2.0, 1.0]
    assert len(t.sent) == 4 and len(set(t.sent)) == 1


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422, 500, 502, 504])
def test_no_other_status_is_retried_even_with_retry_after(status):
    payload = {"error": {"code": "nope", "message": "no"}}
    c, t, waits = scripted((status, payload, {"Retry-After": "1"}), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.status == status and e.value.attempts == 1 and e.value.retry_after == 1.0
    assert waits == [] and len(t.sent) == 1


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("headers", [None, {}, {"Content-Type": "application/json"}, "absent"])
def test_429_and_503_without_retry_after_are_not_retried(status, headers):
    refusal = (status, REFUSALS[status]) if headers == "absent" else (status, REFUSALS[status], headers)
    c, t, waits = scripted(refusal, (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.status == status and e.value.retry_after is None and e.value.attempts == 1
    assert waits == [] and len(t.sent) == 1


def test_a_transport_that_returns_the_two_tuple_works_as_before():
    t = FakeTransport()  # (status, payload), no headers
    assert Client(transport=t).get_action("a1") == {"ok": True, "path": "/v1/actions/a1"}
    refused = FakeTransport(lambda m, p, b: (503, BUSY))
    with pytest.raises(CommitLayerError) as e:
        Client(transport=refused, sleep=lambda s: pytest.fail("slept")).record_action("c", "s")
    assert e.value.code == "busy" and e.value.retry_after is None and e.value.attempts == 1
    assert len(refused.calls) == 1


def test_a_wait_above_the_ceiling_is_neither_waited_nor_retried():
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": "11"}), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.retry_after == 11.0 and e.value.attempts == 1 and waits == [] and len(t.sent) == 1
    # the ceiling itself is still accepted, and a caller may raise it
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": "10"}), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED and waits == [10.0]
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": "11"}), (201, RECORDED), max_retry_wait=30)
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED and waits == [11.0]
    # the Developer plan's daily cap states the seconds to midnight: an error at once, never a wait
    cap = {"error": {"code": "fair_use_exceeded", "message": "250 recorded actions a day"}}
    c, t, waits = scripted((429, cap, {"Retry-After": "41213"}), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.code == "fair_use_exceeded" and e.value.retry_after == 41213.0 and waits == []


def test_max_retries_zero_turns_retrying_off_and_the_limit_is_configurable():
    c, t, waits = scripted((503, BUSY, {"Retry-After": "1"}), (201, RECORDED), max_retries=0)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.attempts == 1 and e.value.retry_after == 1.0 and waits == [] and len(t.sent) == 1

    c, t, waits = scripted((503, BUSY, {"Retry-After": "1"}), max_retries=1)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.attempts == 2 and waits == [1.0] and len(t.sent) == 2

    for bad in (
        {"max_retries": -1},
        {"max_retry_wait": -0.5},
        # not a number of seconds a client can wait: refused when the client is made, not at the first refusal
        {"max_retry_wait": float("nan")},
        {"max_retry_wait": float("inf")},
        {"max_retries": float("nan")},
        {"max_retries": float("inf")},
    ):
        with pytest.raises(ValueError):
            Client(transport=FakeTransport(), **bad)


@pytest.mark.parametrize(
    ("header", "seconds"), [("0", 0.0), ("1", 1.0), ("2.5", 2.5), ("3.", 3.0), (".25", 0.25), (" 4 ", 4.0), (7, 7.0)]
)
def test_delta_seconds_integer_or_decimal_zero_or_more(header, seconds):
    c, t, waits = scripted((503, BUSY, {"Retry-After": header}), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED
    assert waits == [seconds] and len(t.sent) == 2


@pytest.mark.parametrize(
    "header",
    ["soon", "", " ", "-1", "-0.5", "+1", "1e3", "1,5", "1 2", "NaN", "nan", "inf", "Infinity", "0x10", "１２", "5s",
     "2026-11-13T10:00:00Z", "Fri, 32 Nov 2026 10:00:00 GMT", "\x00", True, [1]],
)  # fmt: skip
def test_a_retry_after_that_is_not_a_wait_is_treated_as_absent(header):
    c, t, waits = scripted((503, BUSY, {"Retry-After": header}), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.retry_after is None and e.value.attempts == 1 and waits == [] and len(t.sent) == 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("86400", 86400.0), ("99999999999999999999", 1e20), ("9" * 400, None), ("1" + "0" * 400 + ".5", None)],
)
def test_an_enormous_retry_after_is_never_waited(header, seconds):
    # a number a float cannot hold is not a wait the client can read; either way nothing is slept
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": header}), (201, RECORDED), max_retry_wait=3600)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.retry_after == seconds
    assert e.value.attempts == 1 and waits == [] and len(t.sent) == 1


def test_retry_after_as_an_http_date():
    now = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)
    parse = commitlayer._parse_retry_after
    assert parse("Fri, 13 Nov 2026 10:00:02 GMT", now) == 2.0
    assert parse("Friday, 13-Nov-26 10:00:02 GMT", now) == 2.0  # the obsolete RFC 850 form
    assert parse("Fri Nov 13 10:00:02 2026", now) == 2.0  # asctime: no zone, read as UTC
    assert parse("Fri, 13 Nov 2026 12:00:02 +0200", now) == 2.0
    assert parse("Fri, 13 Nov 2026 09:59:00 GMT", now) == 0.0  # already past: no wait, not a negative one
    assert parse(format_datetime(now + timedelta(days=2), usegmt=True), now) == 172800.0
    assert parse(None, now) is None

    # through the client, against the real clock: a date three seconds ahead is waited (HTTP-dates carry whole
    # seconds, so the wait is somewhere between two and three), one a minute ahead is past the ceiling
    c, t, waits = scripted((503, BUSY, {"Retry-After": formatdate(time.time() + 3, usegmt=True)}), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED
    assert len(waits) == 1 and 1.0 < waits[0] <= 3.0 and len(t.sent) == 2
    c, t, waits = scripted((503, BUSY, {"Retry-After": formatdate(time.time() + 60, usegmt=True)}), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.retry_after is not None and 50.0 < e.value.retry_after <= 60.0 and waits == []
    c, t, waits = scripted((503, BUSY, {"Retry-After": formatdate(time.time() - 60, usegmt=True)}), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED and waits == [0.0]


@pytest.mark.parametrize("name", ["Retry-After", "retry-after", "RETRY-AFTER", "rEtRy-AfTeR"])
def test_the_header_name_is_matched_in_any_case(name):
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Content-Type": "application/json", name: "1"}), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED and waits == [1.0]


def _as_message(cls: type) -> Any:
    message = cls()
    message["Content-Type"] = "application/json"
    message["Retry-After"] = "1"
    return message


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "1"},
        _as_message(http.client.HTTPMessage),  # what urllib gives as response.headers and error.headers
        _as_message(email.message.Message),
        [("Content-Type", "application/json"), ("Retry-After", "1")],
        (("retry-after", "1"),),
        {"Retry-After": "1"}.items(),
        [(b"Retry-After", b"1")],  # raw pairs, as an ASGI or HTTP/2 client holds them
        {b"retry-after": bytearray(b"1")},
    ],
    ids=["dict", "HTTPMessage", "email.Message", "list of pairs", "tuple of pairs", "items view", "bytes", "bytes dict"],
)
def test_a_transport_may_return_its_headers_in_any_of_the_usual_containers(headers):
    c, t, waits = scripted((503, BUSY, headers), (201, RECORDED))
    assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED
    assert waits == [1.0] and len(t.sent) == 2


@pytest.mark.parametrize("headers", [5, 1.5, True, "Retry-After: 1", [1, 2], [("Retry-After",)], object()])
def test_something_that_is_not_headers_is_no_headers(headers):
    c, t, waits = scripted((503, BUSY, headers), (201, RECORDED))
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert e.value.retry_after is None and e.value.attempts == 1 and waits == [] and len(t.sent) == 1


@pytest.mark.parametrize(
    ("stated", "seconds"),
    [(["1", "3600"], 3600.0), (["3600", "1"], 3600.0), (["soon", "2"], 2.0), (["2", "-1"], 2.0), (["x", ""], None)],
)
def test_a_header_stated_twice_is_taken_at_the_longer_wait(stated, seconds):
    # a proxy in front of the API can add a Retry-After of its own; the order the two arrive in decides nothing
    headers = [("Retry-After", value) for value in stated]
    c, t, waits = scripted((503, BUSY, headers), (201, RECORDED))
    if seconds == 2.0:
        assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED and waits == [2.0]
    else:
        with pytest.raises(CommitLayerError) as e:
            c.record_action("refund_workflow", "ord_1", action_id="a")
        assert e.value.retry_after == seconds and e.value.attempts == 1 and waits == [] and len(t.sent) == 1


def test_the_wait_is_spread_upward_never_below_what_was_stated_and_never_past_the_ceiling():
    # between the stated wait and three times it; a jitter that is not a number in [0, 1] adds nothing
    spreads = ((0.0, 2.0), (0.25, 3.0), (0.5, 4.0), (0.999, 5.996), (1.0, 6.0), (True, 6.0), (7.0, 2.0), (-3.0, 2.0),
               (float("nan"), 2.0), (None, 2.0), ("soon", 2.0))  # fmt: skip
    for spread, expected in spreads:
        c, t, waits = scripted((503, BUSY, {"Retry-After": "2"}), (201, RECORDED), jitter=lambda s=spread: s)
        assert c.record_action("refund_workflow", "ord_1", action_id="a") == RECORDED
        assert waits == [pytest.approx(expected)] and waits[0] >= 2.0
    # near the ceiling the addition is a share of what is left under it, so the waits are still spread there
    for spread, expected in ((1.0, 10.0), (0.5, 9.5), (0.0, 9.0)):
        c, t, waits = scripted((503, BUSY, {"Retry-After": "9"}), (201, RECORDED), jitter=lambda s=spread: s)
        c.record_action("refund_workflow", "ord_1", action_id="a")
        assert waits == [expected]
    # the worst case of one call: every wait at the ceiling, max_retries of them
    c, t, waits = scripted((503, BUSY, {"Retry-After": "10"}), jitter=lambda: 1.0)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert waits == [10.0] * 3 and sum(waits) == c.max_retries * c.max_retry_wait and e.value.attempts == 4
    # the default jitter is the standard generator: every wait is the stated second and up to two more, and the
    # waits of a burst are spread over those two seconds, not bunched at their start
    t = Script((503, BUSY, {"Retry-After": "1"}))
    waits = []
    with pytest.raises(CommitLayerError):
        Client(transport=t, sleep=waits.append, max_retries=200).record_action("c", "s", action_id="a")
    assert len(waits) == 200 and all(1.0 <= w < 3.0 for w in waits) and len(set(waits)) > 100
    assert min(waits) < 1.5 and max(waits) > 2.5


def test_a_timeout_or_a_network_error_is_not_retried():
    for failure in (TimeoutError("timed out"), OSError("connection reset"), ConnectionRefusedError("refused")):
        calls: list[str] = []

        def transport(method, path, body, failure=failure, calls=calls):
            calls.append(path)
            raise failure

        c = Client(transport=transport, sleep=lambda s: pytest.fail("slept"))
        with pytest.raises(type(failure)):
            c.record_action("refund_workflow", "ord_1", action_id="a")
        assert calls == ["/v1/actions"]


def test_every_method_goes_through_the_same_rule_with_an_unchanged_request():
    calls = [
        lambda c: c.push_facts([{"source": "crm", "subject_id": "o", "fact_type": "t", "occurred_at": "2026-10-13T10:42:09Z"}]),
        lambda c: c.verify("refund:ord_1"),
        lambda c: c.verify(action={"action_id": "a", "contract_id": "c", "subject_id": "s"}),
        lambda c: c.get_action("refund:ord_1"),
        lambda c: c.wait_for_settlement("refund:ord_1", timeout=5),
        lambda c: c.draft_contract("refund_order"),
        lambda c: c.validate_contract("id: x"),
        lambda c: c.save_contract("id: x"),
        lambda c: c.packs.list(),
        lambda c: c.packs.install("x402_service_delivery", {}, confirm=True),
        lambda c: c.request("POST", "/v1/actions", {"action_id": "a", "contract_id": "c", "subject_id": "s"}),
        lambda c: c.request("DELETE", "/v1/anything"),
    ]  # fmt: skip
    for call in calls:
        c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": "1"}), (200, {"ok": True, "status": "settled"}))
        assert call(c)["ok"] is True
        assert waits == [1.0] and len(t.sent) == 2 and t.sent[0] == t.sent[1]


def test_a_recording_without_an_action_id_is_sent_once_because_a_repeat_could_record_a_second_action():
    # POST /v1/actions may answer 503 busy after the action itself is stored; only the action_id makes a repeat
    # the same action. record_action always sends one; a raw request or an inline verify may not.
    unnamed = [
        lambda c: c.request("POST", "/v1/actions", {"contract_id": "c", "subject_id": "s"}),
        lambda c: c.request("POST", "/v1/actions/", {"contract_id": "c", "subject_id": "s", "action_id": ""}),
        lambda c: c.request("post", "/v1/actions?x=1", {"contract_id": "c", "subject_id": "s", "action_id": None}),
        lambda c: c.verify(action={"contract_id": "c", "subject_id": "s"}),
    ]
    # other spellings of the same route, as a server or a proxy in front of it would read them
    for path in ("/v1/actions#frag", "/v1/%61ctions", "/v1/actions%2F", "/v1//actions", "/v1/./actions", "//v1/actions",
                 "v1/actions", "/v1/actions/?x", "/v1/actions//", "/v1/x/../actions", "/v1/actions/verify/?a=1#b"):  # fmt: skip
        body = {"contract_id": "c", "subject_id": "s"}
        if "verify" in path:
            body = {"action": body}
        unnamed.append(lambda c, path=path, body=body: c.request("POST", path, body))
    for status in (429, 503):
        for call in unnamed:
            c, t, waits = scripted((status, REFUSALS[status], {"Retry-After": "1"}), (201, RECORDED))
            with pytest.raises(CommitLayerError) as e:
                call(c)
            assert e.value.status == status and e.value.attempts == 1 and e.value.retry_after == 1.0
            assert waits == [] and len(t.sent) == 1
    # a dry run stores nothing, so it is repeated whatever it names
    c, t, waits = scripted((503, BUSY, {"Retry-After": "1"}), (200, {"ok": True}))
    assert c.verify(action={"contract_id": "c", "subject_id": "s"}, dry_run=True) == {"ok": True}
    assert waits == [1.0] and t.sent[0] == t.sent[1]
    # and the rule is about that one route: a body without action_id anywhere else is repeated
    for path in ("/v1/facts", "/v1/actions/a1/anything", "/v1/actionsx", "/v2/actions", "/v1/Actions"):
        c, t, waits = scripted((503, BUSY, {"Retry-After": "1"}), (200, {"ok": True}))
        assert c.request("POST", path, {"contract_id": "c", "subject_id": "s"}) == {"ok": True}
        assert waits == [1.0] and t.sent[0] == t.sent[1]


def test_the_decorator_records_after_a_refusal_and_still_runs_the_function_when_retries_run_out(caplog):
    c, t, waits = scripted((429, WORKSPACE_BUSY, {"Retry-After": "1"}), (201, RECORDED))
    ran = []

    @c.action("refund_workflow", subject="order_id", params=lambda amount, **_: {"amount": amount})
    def refund(order_id: str, amount: float) -> str:
        ran.append(order_id)
        return "refunded"

    assert refund("ord_1", 40.0) == "refunded" and ran == ["ord_1"]
    assert refund.last_recorded == RECORDED and waits == [1.0] and t.sent[0] == t.sent[1]

    c, t, waits = scripted((503, BUSY, {"Retry-After": "1"}))

    @c.action("refund_workflow", subject="order_id")
    def cancel(order_id: str) -> str:
        return "cancelled"

    with caplog.at_level(logging.WARNING, logger="commitlayer"):
        assert cancel("ord_2") == "cancelled"  # fail-open: the business action does not depend on the verifier
    assert cancel.last_recorded is None and waits == [1.0] * 3 and len(t.sent) == 4 and len(set(t.sent)) == 1
    assert any("action not recorded for cancel" in r.getMessage() for r in caplog.records)


def test_one_log_line_per_retry_without_the_key_or_the_body(caplog):
    t = Script((429, WORKSPACE_BUSY, {"Retry-After": "1"}), (503, BUSY, {"Retry-After": "2"}), (201, RECORDED))
    c = Client(api_key="local-test-key-never-logged", transport=t, sleep=lambda s: None, jitter=lambda: 0.0)
    with caplog.at_level(logging.INFO, logger="commitlayer"):
        c.record_action("refund_workflow", "subject-in-the-body", params={"card": "4242"}, action_id="a")
    lines = [r.getMessage() for r in caplog.records if r.name == "commitlayer"]
    assert lines == [
        "commitlayer: POST /v1/actions answered 429 workspace_busy; sending it again in 1.00 s (retry 1 of 3)",
        "commitlayer: POST /v1/actions answered 503 busy; sending it again in 2.00 s (retry 2 of 3)",
    ]
    assert all(r.levelno == logging.INFO for r in caplog.records)
    assert "local-test-key-never-logged" not in caplog.text and "subject-in-the-body" not in caplog.text
    assert "4242" not in caplog.text

    # the path is logged without its query, which can carry what a caller put there
    caplog.clear()
    t = Script((429, WORKSPACE_BUSY, {"Retry-After": "1"}), (200, {"status": "settled"}))
    c = Client(transport=t, sleep=lambda s: None, jitter=lambda: 0.0, max_retries=1)
    with caplog.at_level(logging.INFO, logger="commitlayer"):
        c.request("GET", "/v1/actions/a1/wait?timeout=5&note=in-the-query")
    assert [r.getMessage() for r in caplog.records] == [
        "commitlayer: GET /v1/actions/a1/wait answered 429 workspace_busy; sending it again in 1.00 s (retry 1 of 1)"
    ]
    assert t.sent[0][1] == "/v1/actions/a1/wait?timeout=5&note=in-the-query"  # sent whole, logged short


def test_the_error_keeps_its_positional_parameters_and_gains_two_attributes():
    e = CommitLayerError(402, "plan_required", "Builder plan needed", {"upgrade_to": "builder"})
    assert (e.status, e.code, e.message, e.details) == (402, "plan_required", "Builder plan needed", {"upgrade_to": "builder"})
    assert str(e) == "plan_required (402): Builder plan needed" and isinstance(e, RuntimeError)
    assert e.retry_after is None and e.attempts == 1
    e = CommitLayerError(429, "workspace_busy", "busy", retry_after=1.0, attempts=4)
    assert e.details is None and e.retry_after == 1.0 and e.attempts == 4


def test_the_error_can_be_pickled_and_copied_with_everything_it_carries():
    # a process pool sends a worker's exception to the parent pickled; before, rebuilding it raised TypeError
    e = CommitLayerError(429, "workspace_busy", "busy", {"per_workspace": 3}, retry_after=1.0, attempts=4)
    for clone in (pickle.loads(pickle.dumps(e)), copy.copy(e), copy.deepcopy(e)):
        assert type(clone) is CommitLayerError and clone is not e
        assert (clone.status, clone.code, clone.message, clone.details) == (429, "workspace_busy", "busy", {"per_workspace": 3})
        assert (clone.retry_after, clone.attempts) == (1.0, 4) and str(clone) == str(e)
    assert repr(e) == "CommitLayerError('workspace_busy (429): busy')"


def test_the_readme_example_of_retries_runs_and_states_the_defaults(monkeypatch):
    # The block under "What is retried" is what a reader pastes. It must run as written, against a refusal, and
    # the client it calls "the defaults" must be one.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## What is retried", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```python\n(.*?)```", section, re.S)
    assert len(blocks) == 1 and "except CommitLayerError as e:" in blocks[0]
    sent: list[str] = []

    def http(self, method, path, body):
        sent.append(path)
        return 429, WORKSPACE_BUSY, {"Retry-After": "11"}  # past the ceiling: raised at once, nothing is slept

    monkeypatch.setenv("COMMITLAYER_API_KEY", "local-test-key")
    monkeypatch.setattr(Client, "_http", http)
    names: dict[str, Any] = {}
    exec(compile(blocks[0], "README.md", "exec"), names)  # noqa: S102 - the package's own README
    assert sent == ["/v1/actions"]
    default = Client()
    assert (names["cl"].max_retries, names["cl"].max_retry_wait) == (default.max_retries, default.max_retry_wait)
    assert f"max_retries={default.max_retries}, max_retry_wait={default.max_retry_wait}" in blocks[0]


# ---------------- the built-in transport, against a real HTTP server ----------------


class LocalApi:
    """An HTTP server on a loopback port that answers from a list of ``(status, headers, body bytes)`` and keeps
    every request exactly as it arrived. ``headers`` is a dict, or a list of pairs to send one name twice."""

    def __init__(self, *answers: tuple[int, Any, bytes]):
        self.answers = list(answers)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                api.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
                status, headers, body = api.answers.pop(0)
                self.send_response(status)
                for name, value in headers.items() if isinstance(headers, dict) else headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer  # noqa: N815 - http.server naming

            def log_message(self, *args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def local_api(monkeypatch):
    # The loopback server must be reached directly, whatever proxy the environment names.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    started: list[LocalApi] = []

    def start(*answers: tuple[int, Any, bytes]) -> LocalApi:
        started.append(LocalApi(*answers))
        return started[-1]

    yield start
    for api in started:
        api.stop()


def as_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def test_urllib_transport_returns_the_headers_of_a_success_and_of_an_error(local_api):
    api = local_api(
        (201, {"Content-Type": "application/json", "X-Request-Id": "req_1"}, as_json(RECORDED)),
        (503, {"Content-Type": "application/json", "Retry-After": "1"}, as_json(BUSY)),
        (429, {"Content-Type": "application/json", "retry-after": "7"}, as_json(WORKSPACE_BUSY)),
        (503, {"Content-Type": "text/plain"}, b"Service Unavailable"),
        (204, {"X-Empty": "yes"}, b""),
    )
    c = Client(api_key="local-test-key", api_url=api.url, timeout=10)
    header = commitlayer._header

    status, payload, headers = c._http("POST", "/v1/actions", {"action_id": "a"})
    assert (status, payload) == (201, RECORDED) and header(headers, "x-request-id") == "req_1"

    status, payload, headers = c._http("POST", "/v1/actions", {"action_id": "a"})
    assert (status, payload) == (503, BUSY) and header(headers, "retry-after") == "1"

    status, payload, headers = c._http("GET", "/v1/actions/a", None)
    assert (status, payload) == (429, WORKSPACE_BUSY) and header(headers, "retry-after") == "7"

    # an answer that is not JSON (a proxy, the server's connection limit) still comes back with its headers
    status, payload, headers = c._http("POST", "/v1/actions", {"action_id": "a"})
    assert status == 503 and payload == {"error": {"code": "http_error", "message": "Service Unavailable"}}
    assert header(headers, "content-type") == "text/plain" and header(headers, "retry-after") is None

    status, payload, headers = c._http("GET", "/v1/empty", None)
    assert (status, payload) == (204, {}) and header(headers, "x-empty") == "yes"
    assert [r[0] for r in api.requests] == ["POST", "POST", "GET", "POST", "GET"]


def test_urllib_transport_keeps_a_header_the_answer_carries_twice(local_api):
    twice = [("Content-Type", "application/json"), ("Retry-After", "1"), ("Retry-After", "3600")]
    api = local_api((503, twice, as_json(BUSY)), (503, list(reversed(twice)), as_json(BUSY)))
    waits: list[float] = []
    c = Client(api_key="k", api_url=api.url, timeout=10, sleep=waits.append)
    status, payload, headers = c._http("POST", "/v1/actions", {"action_id": "a"})
    assert status == 503 and sorted(commitlayer._header_values(headers, "retry-after")) == ["1", "3600"]
    # whichever comes first, the longer wait is the one stated, and it is past the ceiling: no retry
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert (e.value.retry_after, e.value.attempts) == (3600.0, 1) and waits == [] and len(api.requests) == 2


def test_urllib_transport_retries_a_refused_recording_with_the_same_bytes(local_api):
    api = local_api(
        (429, {"Content-Type": "application/json", "Retry-After": "1"}, as_json(WORKSPACE_BUSY)),
        (503, {"Content-Type": "application/json", "Retry-After": "1"}, as_json(BUSY)),
        (201, {"Content-Type": "application/json"}, as_json(RECORDED)),
    )
    waits: list[float] = []
    c = Client(api_key="local-test-key", api_url=api.url, timeout=10, sleep=waits.append, jitter=lambda: 0.0)
    assert c.record_action("refund_workflow", "ord_1", params={"amount": 40}) == RECORDED
    assert waits == [1.0, 1.0]
    assert len(api.requests) == 3
    bodies = [r[3] for r in api.requests]
    assert bodies[0] == bodies[1] == bodies[2] and json.loads(bodies[0])["action_id"].startswith("act_")
    assert all(r[:2] == ("POST", "/v1/actions") and r[2]["x-api-key"] == "local-test-key" for r in api.requests)


def test_urllib_transport_surfaces_the_last_refusal_and_does_not_retry_a_plain_503(local_api):
    refusal = (503, {"Content-Type": "application/json", "Retry-After": "1"}, as_json(BUSY))
    api = local_api(refusal, refusal)
    waits: list[float] = []
    c = Client(api_key="k", api_url=api.url, timeout=10, max_retries=1, sleep=waits.append, jitter=lambda: 0.0)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert (e.value.status, e.value.code, e.value.retry_after, e.value.attempts) == (503, "busy", 1.0, 2)
    assert waits == [1.0] and len(api.requests) == 2

    api = local_api((503, {"Content-Type": "text/plain"}, b"Service Unavailable"))
    c = Client(api_key="k", api_url=api.url, timeout=10, sleep=waits.append)
    with pytest.raises(CommitLayerError) as e:
        c.record_action("refund_workflow", "ord_1", action_id="a")
    assert (e.value.status, e.value.code, e.value.retry_after, e.value.attempts) == (503, "http_error", None, 1)
    assert waits == [1.0] and len(api.requests) == 1
