"""0.1.4: the canonical environment names (one key per process), the deprecated COMMITLAYER_API_KEY fallback, the
contract default, ``publish_batch`` (``POST /v1/evidence/batch``, mocked here: its contract is the API's), and the
runtime version of the built, installed artifact."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import commitlayer  # noqa: E402
from commitlayer import Client, CommitLayerError  # noqa: E402

KEYS = ("COMMITLAYER_RECORDER_KEY", "COMMITLAYER_EVIDENCE_KEY", "COMMITLAYER_API_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in (*KEYS, "COMMITLAYER_API_URL", "COMMITLAYER_WORKSPACE_ID", "COMMITLAYER_CONTRACT_ID"):
        monkeypatch.delenv(k, raising=False)
    commitlayer._WARNED.clear()


class Fake:
    def __init__(self, answers: dict[str, tuple[int, dict[str, Any]]] | None = None):
        self.calls: list[tuple[str, str, Any]] = []
        self.answers = answers or {}

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        return self.answers.get(path, (200, {"ok": True}))


def test_recorder_and_evidence_read_their_own_key_and_never_the_other(monkeypatch):
    monkeypatch.setenv("COMMITLAYER_RECORDER_KEY", "rec-value")
    monkeypatch.setenv("COMMITLAYER_EVIDENCE_KEY", "ev-value")
    monkeypatch.setenv("COMMITLAYER_API_URL", "http://127.0.0.1:9/")
    assert commitlayer.recorder().api_key == "rec-value"
    assert commitlayer.evidence().api_key == "ev-value"
    assert commitlayer.recorder().api_url == "http://127.0.0.1:9"


def test_a_plain_client_takes_the_one_key_the_process_has_and_refuses_both(monkeypatch):
    monkeypatch.setenv("COMMITLAYER_EVIDENCE_KEY", "ev-only")
    assert Client().api_key == "ev-only"
    monkeypatch.setenv("COMMITLAYER_RECORDER_KEY", "rec")
    with pytest.raises(ValueError, match="separate"):
        Client()


def test_a_recorder_never_falls_back_to_the_evidence_key(monkeypatch):
    monkeypatch.setenv("COMMITLAYER_EVIDENCE_KEY", "ev-only")
    with pytest.raises(ValueError, match="COMMITLAYER_RECORDER_KEY"):
        commitlayer.recorder()


def test_the_old_name_still_works_and_warns_once(monkeypatch):
    monkeypatch.setenv("COMMITLAYER_API_KEY", "legacy")
    with warnings.catch_warnings(record=True) as got:
        warnings.simplefilter("always")
        assert Client().api_key == "legacy"
        assert commitlayer.recorder().api_key == "legacy"
    dep = [w for w in got if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1 and "COMMITLAYER_RECORDER_KEY" in str(dep[0].message)


def test_the_contract_and_workspace_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("COMMITLAYER_CONTRACT_ID", "scop_refund_order")
    monkeypatch.setenv("COMMITLAYER_WORKSPACE_ID", "ten_1")
    t = Fake()
    c = Client(transport=t, role="recorder")
    assert c.workspace_id == "ten_1"
    c.record_action(subject_id="ord_1", action_id="refund-ord_1", params={"amount": 10})
    assert t.calls[0][2]["contract_id"] == "scop_refund_order"
    c.record_action("other_contract", "ord_2", action_id="x")
    assert t.calls[1][2]["contract_id"] == "other_contract"
    monkeypatch.delenv("COMMITLAYER_CONTRACT_ID")
    with pytest.raises(ValueError, match="COMMITLAYER_CONTRACT_ID"):
        Client(transport=t).record_action(subject_id="ord_3")


def test_publish_batch_sends_records_and_checkpoints_for_every_source():
    answer = {
        "facts": [{"fact_id": "fct_1", "status": "accepted"}],
        "checkpoints": [
            {"source": "stripe", "observed_through": "2026-11-18T10:00:00Z", "accepted": True},
            {"source": "shopify", "observed_through": "2026-11-18T10:00:00Z", "accepted": True},
            {"source": "zendesk", "observed_through": "2026-11-18T10:00:00Z", "accepted": True},
        ],
        "affected_actions": ["act_1"],
        "reverify_scheduled": True,
        "snapshot_versions": {"act_1": 3},
    }
    t = Fake({"/v1/evidence/batch": (201, answer)})
    c = Client(transport=t, role="evidence")
    when = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    fact = {"source": "stripe", "subject_id": "pay_1", "fact_type": "refund", "occurred_at": "2026-11-18T09:59:00Z"}
    got = c.publish_batch(facts=[fact], observed_through={"stripe": when, "shopify": when, "zendesk": "2026-11-18T10:00:00Z"})
    assert got == answer
    assert t.calls[0] == (
        "POST",
        "/v1/evidence/batch",
        {
            "facts": [fact],
            "observed_through": {
                "stripe": "2026-11-18T10:00:00Z",
                "shopify": "2026-11-18T10:00:00Z",
                "zendesk": "2026-11-18T10:00:00Z",
            },
        },
    )


def test_a_checkpoint_alone_is_a_valid_batch_and_one_is_required():
    t = Fake({"/v1/evidence/batch": (201, {"facts": [], "checkpoints": []})})
    c = Client(transport=t)
    c.publish_batch(observed_through={"stripe": "2026-11-18T10:00:00Z"})
    assert t.calls == [("POST", "/v1/evidence/batch", {"facts": [], "observed_through": {"stripe": "2026-11-18T10:00:00Z"}})]
    with pytest.raises(ValueError):
        c.publish_batch(facts=[], observed_through={})
    with pytest.raises(ValueError, match="timezone"):
        c.publish_batch(observed_through={"stripe": datetime(2026, 1, 1)})


def test_a_recorder_key_is_refused_by_the_batch_with_a_readable_error():
    refused = {"error": {"code": "insufficient_scope", "message": "This key cannot write facts."}}
    t = Fake({"/v1/evidence/batch": (403, refused)})
    with pytest.raises(CommitLayerError) as e:
        Client(transport=t).publish_batch(observed_through={"stripe": "2026-11-18T10:00:00Z"})
    assert e.value.status == 403 and e.value.code == "insufficient_scope"


def test_the_installed_artifact_reports_its_own_version(tmp_path):
    """The built package, installed on its own, reports the version in pyproject.toml: `__version__` comes from the
    distribution's metadata, never from a constant kept by hand (G5, P3-2)."""
    declared = re.search(r'(?m)^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared is not None
    target = tmp_path / "site"
    built = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-build-isolation", "--target", str(target), str(ROOT)],
        capture_output=True,
        text=True,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    if built.returncode != 0 and "setuptools" in (built.stderr + built.stdout):
        pytest.skip("setuptools>=77 is not installed here; CI installs it (ci.yml)")
    assert built.returncode == 0, built.stderr[-2000:]
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(target)!r}); import commitlayer; print(commitlayer.__version__)"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == declared.group(1)
