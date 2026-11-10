"""Fixtures for the integration tests: the SDK from ``sdk/python``, a fake transport, no servers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "sdk" / "python", ROOT / "integrations" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from commitlayer import Client  # noqa: E402


class FakeTransport:
    """Records every request; answers from a queue of ``(status, payload)`` or by a handler."""

    def __init__(self, *responses: tuple[int, dict[str, Any]], handler=None):
        self.responses = list(responses)
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body))
        if self.handler:
            return self.handler(method, path, body)
        if self.responses:
            return self.responses.pop(0)
        return 201, {"action": {"action_id": (body or {}).get("action_id"), "latest": None}}

    @property
    def posted_actions(self) -> list[dict[str, Any]]:
        return [b for m, p, b in self.calls if m == "POST" and p == "/v1/actions" and b is not None]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(transport: FakeTransport) -> Client:
    return Client(transport=transport)


def verification(
    status: str, outcome: str, policy: str, action_id: str = "a1", **extra: Any
) -> dict[str, Any]:
    """A trimmed ``ActionVerification`` as ``GET /v1/actions/{ref}`` returns it."""
    return {
        "action": {"action_id": action_id, "contract_id": "refund_workflow", "contract_version": 2},
        "contract": {"id": "refund_workflow", "version": 2, "window": "15m", "kind": "action"},
        "status": status,
        "settles_at": "2026-10-15T10:57:00Z",
        "lenses": {
            "outcome": {
                "verdict": outcome,
                "reason_codes": ["effect_observed:stripe.refund_created"],
                "reason_labels": ["Effect observed: stripe refund_created"],
                "verdict_hash": "o" * 8,
            },
            "policy": {
                "verdict": policy,
                "reason_codes": ["limit_exceeded:amount"] if policy == "FAIL" else [],
                "reason_labels": ["Limit exceeded: amount"] if policy == "FAIL" else [],
                "verdict_hash": "p" * 8,
            },
        },
        "summary": {"expected": 4, "observed": 4, "headline": f"{policy} — demo"},
        "facts": [],
        "dry_run": False,
        **extra,
    }
