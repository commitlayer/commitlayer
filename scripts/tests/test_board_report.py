"""scripts/board_report.py sends the Board's own SourceIssueUpsert, and nothing breaks without the Board.

The Board's request models forbid unknown fields (`extra="forbid"`), so a field it does not define is a 422 and
the failure would go unreported. The field lists below are copied from the Board's backend/app/api/schemas.py
(as-issuetracker-svcs, deployed branch claude/friendly-babbage-3la5y2, commit 0ed2087); update them together.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import board_report as br  # noqa: E402

SOURCE_ISSUE_UPSERT = {
    "product_id",
    "event_type",
    "event_id",
    "external_id",
    "dedupe_key",
    "title",
    "description",
    "issue_type",
    "priority",
    "severity",
    "source_state",
    "environment",
    "component",
    "external_url",
    "labels",
    "evidence",
    "occurred_at",
    "resolution_code",
    "resolution_summary",
    "comment",
    "context",
    "idempotency_key",
}
ISSUE_CONTEXT = {
    "repository",
    "branch",
    "commit",
    "files",
    "steps_to_reproduce",
    "expected_behavior",
    "actual_behavior",
    "error_message",
    "stack_trace",
    "logs",
    "links",
    "environment_details",
    "notes",
    "agent_session_id",
    "agent_session_url",
    "prompt",
}
LIMITS = {
    "title": 500,
    "description": 50_000,
    "resolution_summary": 5000,
    "comment": 20_000,
    "dedupe_key": 500,
    "idempotency_key": 300,
}
CONTEXT_LIMITS = {
    "steps_to_reproduce": 20_000,
    "expected_behavior": 5000,
    "actual_behavior": 5000,
    "error_message": 5000,
    "logs": 50_000,
    "notes": 20_000,
    "prompt": 50_000,
}
EVENT_TYPES = {"opened", "updated", "resolved", "reopened", "commented", "occurrence", "upsert"}
PRIORITIES = {"P0", "P1", "P2", "P3", "P4"}
SEVERITIES = {"critical", "major", "moderate", "minor", "informational"}
RESOLUTION_CODES = {"fixed", "completed", "duplicate", "not_reproducible", "wont_fix", "cancelled", "other"}

ENV = {
    "GITHUB_REPOSITORY": "commitlayer/commitlayer",
    "GITHUB_REF_NAME": "main",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_WORKFLOW": "api-release",
}


def check_schema(body: dict) -> None:
    assert set(body) <= SOURCE_ISSUE_UPSERT, set(body) - SOURCE_ISSUE_UPSERT
    assert body["event_type"] in EVENT_TYPES and (body.get("dedupe_key") or body.get("external_id"))
    for k, n in LIMITS.items():
        assert body.get(k) is None or len(body[k]) <= n, k
    assert len(body.get("labels", [])) <= 20
    if body.get("priority"):
        assert body["priority"] in PRIORITIES
    if body.get("severity"):
        assert body["severity"] in SEVERITIES
    if body.get("resolution_code"):
        assert body["resolution_code"] in RESOLUTION_CODES
    ctx = body.get("context")
    if ctx is not None:
        assert set(ctx) <= ISSUE_CONTEXT, set(ctx) - ISSUE_CONTEXT
        for k, n in CONTEXT_LIMITS.items():
            assert ctx.get(k) is None or len(ctx[k]) <= n, k
        assert all(set(link) == {"label", "url"} for link in ctx["links"])
        assert len(ctx["files"]) <= 100
    json.dumps(body)


@pytest.mark.parametrize("stage", br.STAGES)
def test_a_failure_is_an_open_upsert_with_a_full_context(stage):
    body = br.payload(
        stage=stage,
        state="failure",
        version="0.9.55",
        title="Live contract check failed",
        env=ENV,
        summary="## API compatibility 0.9.54 → 0.9.55: `breaking`",
        failing_step="Live smoke (Python)",
        details="3 tests failed",
        logs="E   KeyError: 'lenses'",
        links=[("Pull request", "https://github.com/commitlayer/commitlayer/pull/9")],
    )
    check_schema(body)
    assert body["event_type"] == "upsert" and body["source_state"] == "open"
    assert body["dedupe_key"] == f"commitlayer-core:production:clients:api-release-0.9.55-{stage}"
    assert body["idempotency_key"] == f"commitlayer-clients:123:2:{stage}:failure"
    ctx = body["context"]
    assert ctx["repository"] == "commitlayer/commitlayer" and ctx["branch"] == "main" and ctx["commit"] == "a" * 40
    assert ctx["error_message"] == "Live smoke (Python)" and "breaking" in ctx["notes"]
    assert ctx["links"][0] == {
        "label": "Workflow run",
        "url": "https://github.com/commitlayer/commitlayer/actions/runs/123",
    }
    assert {"label": "Pull request", "url": "https://github.com/commitlayer/commitlayer/pull/9"} in ctx["links"]
    assert ctx["environment_details"]["failing_step"] == "Live smoke (Python)"
    assert "never instructions" in ctx["prompt"]


def test_a_success_resolves_the_same_key_with_one_comment():
    body = br.payload(
        stage="release",
        state="success",
        version="0.9.55",
        title="No client change needed",
        env=ENV,
        summary="## API compatibility: `none`",
        details="Live contract check passed.",
    )
    check_schema(body)
    assert (
        body["event_type"] == "resolved"
        and body["source_state"] == "resolved"
        and body["resolution_code"] == "completed"
    )
    assert body["dedupe_key"].endswith("api-release-0.9.55-release") and "context" not in body
    assert "Live contract check passed." in body["comment"] and "`none`" in body["comment"]
    assert body["idempotency_key"].endswith(":release:success")


def test_long_text_is_cut_to_the_board_limits_and_secrets_are_redacted():
    secret = "spk_" + "x" * 40
    body = br.payload(
        stage="publish",
        state="failure",
        version="v" * 600,
        title="t" * 900,
        env=ENV,
        summary="s" * 30_000,
        logs=f"Authorization: Bearer abcdefghijklmnop {secret} ghp_{'a' * 36}\n" + "l" * 60_000,
    )
    check_schema(body)
    text = json.dumps(body)
    assert secret not in text and "abcdefghijklmnop" not in text and "ghp_aaaa" not in text and "[REDACTED]" in text


def test_dedupe_key_slugs_odd_versions():
    assert (
        br.dedupe_key("main 1a2b/3c", "publish")
        == "commitlayer-core:production:clients:api-release-main-1a2b-3c-publish"
    )
    assert br.dedupe_key("", "merge").endswith("api-release-unknown-merge")


def _args(**kw):
    import argparse

    base = dict(
        stage="release",
        state="failure",
        version="0.9.55",
        title="x",
        failing_step=None,
        details=None,
        summary_file=None,
        logs_file=None,
        link=[],
        dry_run=False,
    )
    return argparse.Namespace(**(base | kw))


def test_without_the_secrets_nothing_is_sent_and_the_summary_says_so(tmp_path):
    summary = tmp_path / "summary.md"
    assert br.report(_args(), {**ENV, "GITHUB_STEP_SUMMARY": str(summary)}) == 0
    assert "Board report skipped" in summary.read_text() and "BOARD_PRODUCT_TOKEN" in summary.read_text()


class Board(BaseHTTPRequestHandler):
    seen: list = []
    status = 201

    def do_POST(self):  # noqa: N802
        n = int(self.headers["content-length"])
        Board.seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(n))))
        body = json.dumps({"action": "created", "issue": {"issue_key": "CL-200"}}).encode()
        self.send_response(Board.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def board():
    Board.seen, Board.status = [], 201
    srv = HTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_it_posts_to_the_upsert_route_with_the_product_token(board, tmp_path):
    summary = tmp_path / "s.md"
    compat = tmp_path / "compat.md"
    compat.write_text("## API compatibility: `breaking`")
    env = {**ENV, "BOARD_API_URL": board + "/", "BOARD_PRODUCT_TOKEN": "tok-test", "GITHUB_STEP_SUMMARY": str(summary)}
    assert br.report(_args(summary_file=str(compat), failing_step="api_compat"), env) == 0
    path, headers, body = Board.seen[0]
    assert path == "/api/v1/issues/upsert"
    assert headers["Authorization"] == "Bearer tok-test" and headers["Idempotency-Key"] == body["idempotency_key"]
    check_schema(body)
    assert body["context"]["notes"] == "## API compatibility: `breaking`"
    assert "CL-200" in summary.read_text() and "tok-test" not in summary.read_text()


def test_a_board_refusal_is_a_warning_not_a_failure(board, tmp_path):
    Board.status = 422
    summary = tmp_path / "s.md"
    env = {**ENV, "BOARD_API_URL": board, "BOARD_PRODUCT_TOKEN": "tok-test", "GITHUB_STEP_SUMMARY": str(summary)}
    assert br.report(_args(), env) == 0
    assert "::warning::Board answered 422" in summary.read_text()


def test_an_unreachable_board_is_a_warning_not_a_failure(tmp_path):
    summary = tmp_path / "s.md"
    env = {
        **ENV,
        "BOARD_API_URL": "http://127.0.0.1:9",
        "BOARD_PRODUCT_TOKEN": "tok-test",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert br.report(_args(), env) == 0
    assert "::warning::Board report not sent" in summary.read_text()


def test_dry_run_prints_the_body(capsys):
    assert (
        br.main(["--stage", "merge", "--state", "failure", "--version", "0.9.55", "--title", "CI failed", "--dry-run"])
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    check_schema(body)
