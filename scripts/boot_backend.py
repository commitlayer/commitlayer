"""Boot the CommitLayer API from a backend checkout for the live contract check, and hand the clients a key.

The live smoke suites (tests/live/) run each package's core calls against a real server instead of a fake
transport, so a change the spec diff misses (a renamed field in a response, a new required parameter enforced in
code, an error envelope that moved) still fails. This script starts that server the way production does
(`python -m app.serve`: API and worker in one process, schema created and the contract library and packs seeded at
start), but with the development settings the backend's own tests use: SQLite in a temporary directory, the
development login, no demo seed, no outbound network checks, no rate limits.

It then signs in through the development login (a fresh workspace), activates the contract the smoke records
against, creates an API key with every scope, and writes the connection details for the suites:

    python scripts/boot_backend.py start --backend ../stateprf-svcs --python ../stateprf-svcs/.venv/bin/python \\
        --env-file build/live.env
    set -a; . build/live.env; set +a
    python -m pytest -q tests/live           # and: node --test tests/live
    python scripts/boot_backend.py stop --env-file build/live.env

Standard library only. The key exists only in this throwaway database and in the env file; it is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONTRACT = "refund_workflow"
EMAIL = "live-smoke@example.com"

# The same switches tests/conftest.py of the backend sets, minus the test-only ones.
SERVER_ENV = {
    "AUTH_MODE": "dev",
    "APP_ENV": "development",
    "SERVE_MODE": "all",
    "UVICORN_WORKERS": "1",
    "SEED_DEMO_ON_START": "false",
    "EGRESS_GUARD": "false",
    "RATE_LIMITS": "false",
    "REAL_WORKFLOW_ONBOARDING_NEW_WORKSPACES": "false",
    "SIGNUP_MX_CHECK": "false",
    "SIGNUP_REPUTATION_CHECK": "false",
}


def interpreter(python: str) -> str:
    """An interpreter path that still works from the backend's directory: a relative path is made absolute (not
    resolved: a virtualenv's python is a symlink, and following it would leave the virtualenv)."""
    return os.path.abspath(python) if os.sep in python else python


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - loopback only
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")[:300]}


def wait_healthy(url: str, proc: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the backend exited with {proc.returncode} before it was healthy")
        try:
            status, _ = http("GET", url + "/health")
            if status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise SystemExit(f"the backend was not healthy after {timeout:.0f} s")


def provision(url: str, contract: str) -> str:
    """A fresh workspace with `contract` active and a key with every scope. Returns the key."""
    status, sess = http(
        "POST", url + "/v1/auth/dev-login", {"email": EMAIL, "tenant_name": "Live smoke", "plan": "enterprise"}
    )
    if status != 200 or "token" not in sess:
        raise SystemExit(f"dev-login answered {status}: {str(sess)[:300]}")
    auth = {"authorization": f"Bearer {sess['token']}"}
    status, body = http("POST", f"{url}/v1/contracts/{contract}/select", None, auth)
    if status != 200:
        raise SystemExit(f"selecting {contract} answered {status}: {str(body)[:300]}")
    status, key = http("POST", url + "/v1/settings/api-keys", {"name": "live-smoke"}, auth)
    if status != 200 or "key" not in key:
        raise SystemExit(f"creating the API key answered {status}")
    return key["key"]


def start(args: argparse.Namespace) -> int:
    backend = Path(args.backend).resolve()
    env_file = Path(args.env_file).resolve()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="commitlayer-live-"))
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        **SERVER_ENV,
        "PORT": str(port),
        "DATABASE_URL": f"sqlite:///{data}/live.db",
        "OBJECT_STORE_ROOT": f"{data}/objects",
        "SESSION_SECRET": "live-smoke-session-secret-not-a-real-one",
    }
    log = open(args.log or data / "backend.log", "wb")  # noqa: SIM115 - the server outlives this process
    proc = subprocess.Popen(
        [interpreter(args.python), "-m", "app.serve"],
        cwd=backend,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        wait_healthy(url, proc, args.timeout)
        key = provision(url, args.contract)
    except BaseException:
        os.killpg(proc.pid, signal.SIGTERM)
        raise
    env_file.write_text(
        "\n".join(
            [
                "COMMITLAYER_LIVE=1",
                f"COMMITLAYER_API_URL={url}",
                f"COMMITLAYER_API_KEY={key}",
                f"COMMITLAYER_LIVE_CONTRACT={args.contract}",
                f"COMMITLAYER_LIVE_PID={proc.pid}",
                f"COMMITLAYER_LIVE_LOG={args.log or data / 'backend.log'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    print(f"backend from {backend} is up at {url} (pid {proc.pid}); connection details in {env_file}")
    return 0


def stop(args: argparse.Namespace) -> int:
    path = Path(args.env_file)
    if not path.exists():
        return 0
    values = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
    pid = int(values.get("COMMITLAYER_LIVE_PID", "0") or 0)
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Boot the CommitLayer API for the live smoke suites.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--backend", required=True, help="the stateprf-svcs checkout")
    s.add_argument("--python", default=sys.executable, help="the interpreter with the backend installed")
    s.add_argument("--env-file", required=True)
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--contract", default=CONTRACT)
    s.add_argument("--timeout", type=float, default=120.0)
    s.add_argument("--log", default=None)
    t = sub.add_parser("stop")
    t.add_argument("--env-file", required=True)
    args = ap.parse_args(argv)
    return start(args) if args.cmd == "start" else stop(args)


if __name__ == "__main__":
    sys.exit(main())
