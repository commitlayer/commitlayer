"""The shared verdict summary and the polling loop."""

from __future__ import annotations

import pytest
from commitlayer_integrations.verdict import poll_until_settled, summarize_verification
from conftest import verification


def test_summarize_settled_pass():
    v = summarize_verification(verification("settled", "PASS", "PASS", action_id="act_1"))
    assert v["action_id"] == "act_1"
    assert v["contract_id"] == "refund_workflow" and v["contract_version"] == 2
    assert v["status"] == "settled" and v["settled"] is True and v["passed"] is True
    assert v["outcome"] == "PASS" and v["policy"] == "PASS"
    assert v["reason_codes"]["outcome"] == ["effect_observed:stripe.refund_created"]
    assert v["headline"] == "PASS — demo" and v["settles_at"] == "2026-10-15T10:57:00Z"
    assert v["verdict_hash"] == "p" * 8 and v["timed_out"] is False


def test_summarize_policy_fail_is_not_passed():
    v = summarize_verification(verification("settled", "PASS", "FAIL"))
    assert v["passed"] is False and v["policy"] == "FAIL"
    assert v["reason_codes"]["policy"] == ["limit_exceeded:amount"]


def test_summarize_pending_pass_is_not_passed():
    v = summarize_verification(verification("pending", "PASS", "PASS"))
    assert v["settled"] is False and v["passed"] is False


def test_summarize_never_verified_action():
    v = summarize_verification({"action": {"action_id": "a", "contract_id": "c", "latest": None}})
    assert v["status"] == "unverified" and v["outcome"] is None and v["passed"] is False


def test_summarize_bare_action_with_latest():
    v = summarize_verification(
        {
            "action_id": "a",
            "contract_id": "c",
            "latest": {"outcome": "FAIL", "policy": "FAIL", "status": "settled"},
        }
    )
    assert v["status"] == "settled" and v["outcome"] == "FAIL" and v["passed"] is False


def test_poll_settles_on_third_fetch():
    payloads = iter(
        [
            verification("pending", "UNKNOWN", "UNKNOWN"),
            verification("pending", "UNKNOWN", "UNKNOWN"),
            verification("settled", "PASS", "PASS"),
        ]
    )
    clock = [0.0]
    sleeps: list[float] = []
    polled: list[str] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    v = poll_until_settled(
        lambda: next(payloads),
        timeout_seconds=60,
        poll_seconds=5,
        sleep=sleep,
        clock=lambda: clock[0],
        on_poll=lambda x: polled.append(x["status"]),
    )
    assert v["passed"] is True and v["timed_out"] is False
    assert sleeps == [5, 5] and polled == ["pending", "pending", "settled"]


def test_poll_times_out_while_pending():
    clock = [0.0]
    fetches = [0]

    def fetch():
        fetches[0] += 1
        return verification("pending", "UNKNOWN", "UNKNOWN")

    def sleep(s: float) -> None:
        clock[0] += s

    v = poll_until_settled(fetch, timeout_seconds=12, poll_seconds=5, sleep=sleep, clock=lambda: clock[0])
    assert v["timed_out"] is True and v["settled"] is False and v["passed"] is False
    assert fetches[0] == 4  # t=0, 5, 10; the last sleep is capped at the 2s remaining, then one look at t=12


def test_poll_stops_early_when_asked():
    stop = [False]

    def fetch():
        stop[0] = True
        return verification("pending", "UNKNOWN", "UNKNOWN")

    v = poll_until_settled(fetch, timeout_seconds=600, sleep=lambda s: None, should_stop=lambda: stop[0])
    assert v["timed_out"] is True


def test_poll_propagates_fetch_errors():
    def fetch():
        raise RuntimeError("unreachable")

    with pytest.raises(RuntimeError):
        poll_until_settled(fetch, timeout_seconds=1, sleep=lambda s: None)
