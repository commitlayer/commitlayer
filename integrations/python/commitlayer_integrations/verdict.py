"""The verdict a workflow waits for, reduced to what a completion gate needs.

``GET /v1/actions/{ref}`` returns a full ``ActionVerification`` (lenses, checks, evidence, facts). A workflow
engine stores activity and step results in its history, so the integrations hand back a small, JSON-serialisable
summary instead: both verdicts, the settlement status, the reason codes and the headline. The evidence stays
on the CommitLayer side, where it belongs: the workflow reads the verdict, it never carries the proof.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypedDict


class Verdict(TypedDict):
    action_id: str | None
    contract_id: str | None
    contract_version: int | None
    status: str  # "pending" | "settled" | "unverified"
    settled: bool
    outcome: str | None  # "PASS" | "FAIL" | "UNKNOWN"
    policy: str | None  # "PASS" | "FAIL" | "UNKNOWN"
    passed: bool  # both lenses PASS and settled
    reason_codes: dict[str, list[str]]
    reason_labels: dict[str, list[str]]
    headline: str | None
    settles_at: str | None
    verdict_hash: str | None
    timed_out: bool


def _lens(lenses: Any, name: str) -> dict[str, Any]:
    lens = lenses.get(name) if isinstance(lenses, dict) else None
    return lens if isinstance(lens, dict) else {}


def summarize_verification(payload: dict[str, Any]) -> Verdict:
    """Reduce an ``ActionVerification`` (or a bare ``Action``) to a :class:`Verdict`.

    ``status`` is ``settled`` once the window has closed and every source has caught up, ``pending`` while the
    verdict is provisional, and ``unverified`` when the action has never been verified (``latest`` is null).
    ``passed`` is true only for a **settled** verdict that is PASS under both lenses; a provisional PASS is not
    a completion signal.
    """
    action = payload.get("action") if isinstance(payload.get("action"), dict) else payload
    lenses = payload.get("lenses") or {}
    outcome, policy = _lens(lenses, "outcome"), _lens(lenses, "policy")
    latest = action.get("latest") if isinstance(action.get("latest"), dict) else {}
    outcome_verdict = outcome.get("verdict") or latest.get("outcome")
    policy_verdict = policy.get("verdict") or latest.get("policy")
    status = payload.get("status") or latest.get("status") or ("unverified" if not lenses else "pending")
    settled = status == "settled"
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return Verdict(
        action_id=action.get("action_id"),
        contract_id=action.get("contract_id") or contract.get("id"),
        contract_version=action.get("contract_version") or contract.get("version"),
        status=status,
        settled=settled,
        outcome=outcome_verdict,
        policy=policy_verdict,
        passed=bool(settled and outcome_verdict == "PASS" and policy_verdict == "PASS"),
        reason_codes={
            "outcome": list(outcome.get("reason_codes") or []),
            "policy": list(policy.get("reason_codes") or []),
        },
        reason_labels={
            "outcome": list(outcome.get("reason_labels") or []),
            "policy": list(policy.get("reason_labels") or []),
        },
        headline=summary.get("headline"),
        settles_at=payload.get("settles_at") or latest.get("settles_at"),
        verdict_hash=policy.get("verdict_hash"),
        timed_out=False,
    )


def fetch_verification(client: Any, action_ref: str, hold_seconds: float = 0.0) -> dict[str, Any]:
    """One read of the verification. With ``hold_seconds`` the API's completion gate
    (``GET /v1/actions/{ref}/wait?timeout=``) holds the request until the verdict settles or that many seconds
    pass (60 at most), so a poll returns the moment the last expected effect lands; without it, a plain
    ``GET /v1/actions/{ref}``."""
    hold = max(0, min(60, int(hold_seconds)))
    if hold <= 0:
        return client.get_action(action_ref)
    return client.request("GET", f"/v1/actions/{action_ref}/wait?timeout={hold}")


def poll_until_settled(
    fetch: Callable[[], dict[str, Any]],
    *,
    timeout_seconds: float,
    poll_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_poll: Callable[[Verdict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Verdict:
    """Call ``fetch`` (``GET /v1/actions/{ref}``) until the verification settles or ``timeout_seconds`` pass.

    Returns the last :class:`Verdict` seen, with ``timed_out`` set when the deadline passed first. ``on_poll``
    runs after every fetch (a Temporal heartbeat, a log line); ``should_stop`` is checked before every sleep and
    ends the wait early with ``timed_out: True`` (a cancellation request). Errors from ``fetch`` propagate: a
    verifier that cannot be reached is not a verdict.
    """
    deadline = clock() + timeout_seconds
    while True:
        verdict = summarize_verification(fetch())
        if on_poll:
            on_poll(verdict)
        if verdict["settled"]:
            return verdict
        remaining = deadline - clock()
        if remaining <= 0 or (should_stop and should_stop()):
            verdict["timed_out"] = True
            return verdict
        sleep(min(poll_seconds, remaining))
