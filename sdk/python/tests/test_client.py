"""The Python SDK over a fake transport: the bodies it sends, the version it reports, the pack helpers."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == meta["project"]["version"]


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
