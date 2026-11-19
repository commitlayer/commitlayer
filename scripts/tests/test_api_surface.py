"""The manifest in scripts/api_surface.py says what the code does, both ways.

If a package starts calling an endpoint, sending a field or reading one that the manifest does not list, a test
here fails; so does a manifest entry the code no longer backs. The compatibility check is only as good as this.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in ("scripts", "sdk/python", "recorder/python", "integrations/python"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))

import api_surface as surf  # noqa: E402


def _manifest(package: str) -> set[tuple[str, str]]:
    return {(u.method, surf.normalise_path(u.path)) for u in surf.uses(package)}


@pytest.mark.parametrize("package", sorted(surf.PACKAGES))
def test_every_call_site_is_in_the_manifest_and_every_entry_has_one(package):
    sites = surf.call_sites(package)
    listed = _manifest(package)
    listed_paths = {p for _, p in listed}
    unlisted = sorted((m or "?", p) for m, p in sites if ((m, p) not in listed if m else p not in listed_paths))
    assert not unlisted, f"{package} calls endpoints the manifest does not list: {unlisted}"
    site_paths = {p for m, p in sites if m is None}
    unbacked = sorted(e for e in listed if e not in sites and e[1] not in site_paths)
    assert not unbacked, f"{package}: manifest entries with no call site in the code: {unbacked}"


@pytest.mark.parametrize("use", surf.USES, ids=lambda u: f"{u.package}:{u.method}:{u.path}")
def test_every_listed_field_is_named_in_the_package_source(use):
    text = "\n".join(f.read_text(encoding="utf-8") for f in surf.source_files(use.package))
    if use.package == "integrations-python" or use.package == "integrations-js":
        sdk = "sdk/python/commitlayer" if use.package.endswith("python") else "sdk/js/src"
        text += "\n".join(
            f.read_text(encoding="utf-8") for f in (ROOT / sdk).rglob("*.*") if f.suffix in (".py", ".ts")
        )
    names = {f.split(".")[-1].removesuffix("[]") for f in use.request + use.response + use.query}
    missing = sorted(n for n in names if n not in text)
    assert not missing, f"{use.package} {use.method} {use.path}: {missing} never appear in the source"
    assert set(use.required) <= set(use.request)


def test_inventory_is_deterministic_json():
    a = json.dumps(surf.inventory(), sort_keys=True)
    assert a == json.dumps(surf.inventory(), sort_keys=True)
    assert {p["package"] for p in json.loads(a)["packages"]} == set(surf.PACKAGES)
    assert "| sdk-python | POST | `/v1/actions` |" in surf.to_markdown(surf.inventory())


def test_normalise_path():
    assert surf.normalise_path("/v1/packs/${encodeURIComponent(packId)}/configure") == "/v1/packs/{}/configure"
    assert surf.normalise_path("/v1/actions/{action_ref}/wait?timeout={budget:.0f}") == "/v1/actions/{}/wait"
    assert surf.normalise_path("/v1/actions") == "/v1/actions"


# ---------------- what the Python code actually sends ----------------


class Capture:
    def __init__(self, answer: dict[str, Any] | None = None):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.answer = answer or {}

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        return 200, dict(self.answer)


def _matches(template: str, path: str) -> bool:
    t, p = surf.normalise_path(template).split("/"), path.split("?", 1)[0].split("/")
    return len(t) == len(p) and all(a == "{}" or a == b for a, b in zip(t, p, strict=True))


def _use(package: str, method: str, path: str) -> surf.Use:
    found = [u for u in surf.uses(package) if u.method == method and (u.path == path or _matches(u.path, path))]
    assert len(found) == 1, f"{package}: {method} {path} matches {len(found)} manifest entries"
    return found[0]


def _query(path: str) -> set[str]:
    q = path.split("?", 1)[1] if "?" in path else ""
    return {kv.split("=", 1)[0] for kv in q.split("&") if kv}


def _sdk_calls(full: bool) -> list[tuple[str, str, dict[str, Any] | None]]:
    from commitlayer import Client

    t = Capture({"timed_out": False})
    c = Client(transport=t, agent_id="a" if full else None)
    if full:
        c.record_action(
            "c",
            "s",
            params={"x": 1},
            action_id="i",
            action_type="t",
            subject_kind="k",
            workflow_id="w",
            requested_at="2026-01-01T00:00:00Z",
            channel="api",
            compensates="o",
            contract_version=2,
        )
        c.push_facts([{"source": "x"}], links=[{"a": 1}], watermarks={"x": "2026-01-01T00:00:00Z"})
        c.publish_batch(facts=[{"source": "x"}], observed_through={"x": "2026-01-01T00:00:00Z"})
        every = {
            "links": [{"a": 1}],
            "observed_through": {"x": "2026-01-01T00:00:00Z"},
            "action_id": "i",
            "contract_id": "c",
            "workspace_id": "w",
        }
        c.preflight_batches([{"source": "x"}], **every)
        c.publish_batches([{"source": "x"}], **every)
        c.verify("i", action={"action_id": "j"}, facts=[{"source": "x"}], links=[{"a": 1}], dry_run=True)
        c.packs.dry_run("p", {"w": {}}, workflow="w")
    else:
        c.record_action("c", "s")
        c.push_facts([])
        c.publish_batch(observed_through={"x": "2026-01-01T00:00:00Z"})
        c.preflight_batches()
        c.publish_batches(observed_through={"x": "2026-01-01T00:00:00Z"})
        c.verify()
        c.packs.dry_run("p")
    c.get_action("i")
    c.wait_for_settlement("i", timeout=1)
    c.draft_contract("t")
    c.validate_contract("y")
    c.save_contract("y")
    c.packs.list()
    c.packs.get("p")
    c.packs.configure("p")
    c.packs.install("p", confirm=True)
    c.packs.coverage("p")
    c.packs.recorder_mapping("p")
    return t.calls


def test_the_python_sdk_sends_exactly_the_listed_fields_with_every_option_set():
    seen = set()
    for method, path, body in _sdk_calls(full=True):
        u = _use("sdk-python", method, path)
        seen.add((u.method, u.path))
        assert set(body or {}) == set(u.request), f"{method} {path}"
        assert _query(path) == set(u.query), f"{method} {path}"
    assert seen == {(u.method, u.path) for u in surf.uses("sdk-python")}


def test_the_python_sdk_always_sends_the_listed_required_fields():
    for method, path, body in _sdk_calls(full=False):
        u = _use("sdk-python", method, path)
        assert set(body or {}) == set(u.required), f"{method} {path}"


def test_the_sdks_share_one_surface():
    strip = lambda us: {(u.method, u.path, u.request, u.required, u.query, u.response) for u in us}  # noqa: E731
    assert strip(surf.uses("sdk-python")) == strip(surf.uses("sdk-js"))


def test_the_python_recorder_sends_exactly_the_listed_fields():
    from commitlayer_mcp_recorder import build_action, load_mapping

    u = _use("recorder-python", "POST", "/v1/actions")
    full = load_mapping(
        {
            "tools": {
                "t": {
                    "contract_id": "c",
                    "subject": "arguments.id",
                    "contract_version": 3,
                    "action_type": "a",
                    "subject_kind": "k",
                    "params": {"n": "arguments.n"},
                    "compensates": "arguments.prev",
                }
            }
        }
    ).lookup("t")
    call = {"name": "t", "arguments": {"id": "1", "n": 2, "prev": "p"}}
    body = build_action("t", call, full, request_id=1, agent_id="ag", workflow_id="wf")
    assert set(body) == set(u.request)
    bare = load_mapping({"tools": {"t": {"contract_id": "c", "subject": "arguments.id"}}}).lookup("t")
    assert set(build_action("t", call, bare, request_id=1)) == set(u.required)


def test_the_python_integrations_send_a_subset_of_what_the_manifest_lists():
    from commitlayer_temporal.mapping import ActivityMapping, TemporalRef, build_action_body

    u = _use("integrations-python", "POST", "/v1/actions")
    m = ActivityMapping("c", "args.0", action_type="a", subject_kind="k", compensates="args.1", contract_version=2)
    body = build_action_body(m, ref=TemporalRef("wf", "1", 1, "act"), args=["s", "p"], agent_id="ag")
    assert set(u.required) <= set(body) <= set(u.request)


# ---------------- what the Python verdict readers actually read ----------------


class Tracked(dict):
    """A dict that records every key read through get() or [], with its dotted path."""

    def __init__(self, data: dict[str, Any], seen: set[str], prefix: str = ""):
        super().__init__(
            {k: (Tracked(v, seen, f"{prefix}{k}.") if isinstance(v, dict) else v) for k, v in data.items()}
        )
        self._seen, self._prefix = seen, prefix

    def get(self, key, default=None):
        self._seen.add(self._prefix + str(key))
        return super().get(key, default)

    def __getitem__(self, key):
        self._seen.add(self._prefix + str(key))
        return super().__getitem__(key)


def leaves(paths: set[str]) -> set[str]:
    return {p for p in paths if not any(q.startswith(p + ".") for q in paths)}


FULL_VERIFICATION = {
    "action": {
        "action_id": "a",
        "contract_id": "c",
        "contract_version": 1,
        "latest": {"outcome": "PASS", "policy": "PASS", "status": "settled", "settles_at": "t"},
    },
    "contract": {"id": "c", "version": 1},
    "lenses": {
        "outcome": {"verdict": "PASS", "reason_codes": [], "reason_labels": []},
        "policy": {"verdict": "PASS", "reason_codes": [], "reason_labels": [], "verdict_hash": "h"},
    },
    "status": "settled",
    "summary": {"headline": "h"},
    "settles_at": "t",
    "timed_out": False,
}


def test_the_python_verdict_reader_reads_exactly_the_listed_fields():
    from commitlayer_integrations.verdict import summarize_verification

    # Every field present, and every fallback empty, so each read is reached.
    seen: set[str] = set()
    summarize_verification(Tracked(FULL_VERIFICATION, seen))
    seen_fallbacks: set[str] = set()
    fallbacks = {
        "action": {"latest": FULL_VERIFICATION["action"]["latest"]},
        "contract": FULL_VERIFICATION["contract"],
        "lenses": {"outcome": {}, "policy": {}},
    }
    summarize_verification(Tracked(fallbacks, seen_fallbacks))
    got = leaves(seen | seen_fallbacks)
    listed = set(_use("integrations-python", "GET", "/v1/actions/{action_ref}").response)
    assert got == listed


def test_the_python_sdk_reads_timed_out_from_the_gate():
    from commitlayer import Client

    seen: set[str] = set()
    Client(
        transport=lambda m, p, b: (200, Tracked({"timed_out": False, "status": "settled"}, seen))
    ).wait_for_settlement("a", timeout=0)
    assert leaves(seen) == set(_use("sdk-python", "GET", "/v1/actions/{action_ref}/wait").response)
