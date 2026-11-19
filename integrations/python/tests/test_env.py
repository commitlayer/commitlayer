"""The key's environment names. The integrations read no environment themselves: they record through the client
they are given, and the documented client is ``commitlayer.recorder()``, which reads COMMITLAYER_RECORDER_KEY, falls
back to the deprecated COMMITLAYER_API_KEY with a warning, and never reads COMMITLAYER_EVIDENCE_KEY. These tests
hold the examples to those names and check the documented client behaves so with every integration's recorder."""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

import commitlayer
import pytest
from commitlayer_integrations.tools import ToolRecorder

PACKAGE = Path(__file__).resolve().parents[1]
NAMES = (
    "COMMITLAYER_RECORDER_KEY",
    "COMMITLAYER_EVIDENCE_KEY",
    "COMMITLAYER_API_KEY",
    "COMMITLAYER_API_URL",
    "COMMITLAYER_WORKSPACE_ID",
    "COMMITLAYER_CONTRACT_ID",
)
MAPPING = {"refund_payment": {"contract_id": "refund_workflow", "subject": "arguments.order_id"}}
DEPRECATION = "COMMITLAYER_API_KEY is deprecated; set COMMITLAYER_RECORDER_KEY"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Exactly the CommitLayer variables a test sets, a fresh once-per-process warning, and every request the
    client would send over HTTP captured with the key it carries."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(commitlayer, "_WARNED", set())
    sent: list[dict[str, Any]] = []

    def http(self: commitlayer.Client, method: str, path: str, body: dict[str, Any] | None):
        sent.append({"key": self.api_key, "url": self.api_url + path})
        return 201, {"action": {"action_id": (body or {}).get("action_id"), "latest": None}}

    monkeypatch.setattr(commitlayer.Client, "_http", http)

    def setenv(**values: str) -> list[dict[str, Any]]:
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        return sent

    return setenv


def _record_one() -> None:
    recorder = ToolRecorder(commitlayer.recorder(), MAPPING, fail_open=False)
    assert recorder.record("refund_payment", {"order_id": "o1"}, call_id="c1") is not None


def test_the_recorder_key_is_read_from_commitlayer_recorder_key(env):
    sent = env(COMMITLAYER_RECORDER_KEY="rec-key", COMMITLAYER_API_URL="http://env.invalid")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _record_one()
    assert sent == [{"key": "rec-key", "url": "http://env.invalid/v1/actions"}]


def test_commitlayer_api_key_still_works_with_a_deprecation_warning(env):
    sent = env(COMMITLAYER_API_KEY="legacy-key")
    with pytest.warns(DeprecationWarning, match=DEPRECATION):
        _record_one()
    assert [s["key"] for s in sent] == ["legacy-key"]


def test_commitlayer_recorder_key_wins_over_the_deprecated_name(env):
    sent = env(COMMITLAYER_RECORDER_KEY="rec-key", COMMITLAYER_API_KEY="legacy-key")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _record_one()
    assert [s["key"] for s in sent] == ["rec-key"]


def test_the_evidence_key_is_never_used_for_recording(env):
    env(COMMITLAYER_EVIDENCE_KEY="evidence-key")
    with pytest.raises(ValueError, match="COMMITLAYER_RECORDER_KEY"):
        commitlayer.recorder()
    sent = env(COMMITLAYER_RECORDER_KEY="rec-key")
    _record_one()
    assert [s["key"] for s in sent] == ["rec-key"]


def test_an_explicit_key_wins(env):
    sent = env(COMMITLAYER_RECORDER_KEY="rec-key", COMMITLAYER_API_KEY="legacy-key")
    recorder = ToolRecorder(commitlayer.recorder(api_key="explicit-key"), MAPPING, fail_open=False)
    recorder.record("refund_payment", {"order_id": "o1"}, call_id="c1")
    assert [s["key"] for s in sent] == ["explicit-key"]


def _docs() -> dict[str, str]:
    """Every README and module docstring the distribution ships."""
    files = [*PACKAGE.glob("README.md"), *PACKAGE.glob("commitlayer_*/README.md"), *PACKAGE.glob("commitlayer_*/*.py")]
    return {str(p.relative_to(PACKAGE)): p.read_text() for p in sorted(files)}


def test_the_examples_use_the_canonical_names():
    docs = _docs()
    for name, text in docs.items():
        for line in text.splitlines():
            if "COMMITLAYER_API_KEY" in line:
                assert "deprecated" in line, f"{name}: {line.strip()}"
        # a client built from the environment in an example is a recorder, never a bare Client() that could pick
        # up an evidence key
        assert not re.search(r"\bClient\(\)", text), name
        assert not re.search(r"ToolRecorder\(Client\(", text), name
    exported = [n for n, t in docs.items() if "export COMMITLAYER_RECORDER_KEY=" in t]
    assert sorted(exported) == sorted(
        n for n, t in docs.items() if n.endswith("README.md") and re.search(r"(?m)^export COMMITLAYER_\w+=", t)
    )
    assert "COMMITLAYER_EVIDENCE_KEY" not in "".join(t for n, t in docs.items() if n.endswith(".py"))
