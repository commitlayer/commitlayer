"""``commitlayer-mcp-recorder``: the stdio proxy.

    commitlayer-mcp-recorder --map mapping.json [--agent-id ID] [--workflow-id ID] [--api-url URL]
        [--timeout 5] [--no-fail-open] [--warn-unmapped] -- <downstream command and args>

Client stdin is pumped line by line to the downstream server's stdin and the server's stdout back to the
client's stdout, in order and unchanged. The server's stderr is relayed to our stderr. The proxy exits when
the client closes stdin or the server exits, with the server's exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from typing import Any, BinaryIO

from . import (
    DEFAULT_API_URL,
    MappingError,
    Recorder,
    __version__,
    load_mapping,
    log_to_stderr,
)

GRACE_SECONDS = 5.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commitlayer-mcp-recorder",
        description=(
            "Forward MCP stdio traffic to a downstream server and record mapped tools/call requests as "
            "CommitLayer actions before they are forwarded. The downstream command follows '--'."
        ),
        epilog=(
            "Environment: COMMITLAYER_API_KEY (required to record; without it the proxy is a passthrough), "
            f"COMMITLAYER_API_URL (default {DEFAULT_API_URL}), COMMITLAYER_RECORDER_MAP (default for --map)."
        ),
    )
    parser.add_argument(
        "--map",
        default=os.environ.get("COMMITLAYER_RECORDER_MAP"),
        help="mapping file (JSON, or YAML when PyYAML is installed); default $COMMITLAYER_RECORDER_MAP",
    )
    parser.add_argument("--agent-id", help="overrides agent_id from the mapping file")
    parser.add_argument("--workflow-id", help="overrides workflow_id from the mapping file")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("COMMITLAYER_API_URL") or DEFAULT_API_URL,
        help=f"CommitLayer API base URL; default $COMMITLAYER_API_URL or {DEFAULT_API_URL}",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the API (default 5)")
    parser.add_argument(
        "--no-fail-open",
        action="store_true",
        help="when recording fails, answer the client with a JSON-RPC error instead of forwarding the call",
    )
    parser.add_argument(
        "--warn-unmapped",
        action="store_true",
        help="log each tool name that is called but not in the mapping, once",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def run_proxy(
    command: list[str],
    recorder: Recorder,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    log: Any = log_to_stderr,
) -> int:
    """Run ``command`` as the downstream server and pump traffic until the client or the server is done.
    Returns the exit code to leave with (the server's)."""
    client_in = stdin or sys.stdin.buffer
    client_out = stdout or sys.stdout.buffer
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        log(f"cannot start downstream server {command[0]!r}: {e.strerror or e}")
        return 127
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    child_in, child_out, child_err = child.stdin, child.stdout, child.stderr
    out_lock = threading.Lock()

    def emit(data: bytes) -> None:
        with out_lock:
            client_out.write(data)
            client_out.flush()

    def pump_client() -> None:
        try:
            for line in iter(client_in.readline, b""):
                decision = recorder.before_forward(_parse(line))
                if decision.forward:
                    child_in.write(line)
                    child_in.flush()
                elif decision.error is not None:
                    emit(json.dumps(decision.error, separators=(",", ":")).encode() + b"\n")
        except (OSError, ValueError):
            pass  # the server went away; the main thread collects its exit code
        finally:
            try:
                child_in.close()
            except OSError:
                pass
            try:
                child.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log("downstream server did not exit after the client closed stdin; terminating it")
                child.terminate()

    def pump_server() -> None:
        try:
            for line in iter(child_out.readline, b""):
                recorder.after_response(_parse(line))
                emit(line)
        except OSError:
            pass  # the client went away

    def pump_stderr() -> None:
        try:
            for chunk in iter(lambda: child_err.read1(65536), b""):
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
        except OSError:
            pass

    threads = [
        threading.Thread(target=pump_client, name="client->server", daemon=True),
        threading.Thread(target=pump_server, name="server->client", daemon=True),
        threading.Thread(target=pump_stderr, name="server-stderr", daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        child.wait()
    except KeyboardInterrupt:
        child.terminate()
        child.wait()
    for t in threads[1:]:
        t.join(timeout=GRACE_SECONDS)
    code = child.returncode
    return code if code >= 0 else 128 - code


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if "--" in args_in:
        split = args_in.index("--")
        options, command = args_in[:split], args_in[split + 1 :]
    else:
        options, command = args_in, []
    parser = build_parser()
    args = parser.parse_args(options)
    if not command:
        parser.error("the downstream server command is required after '--'")
    if not args.map:
        parser.error("--map is required (or set COMMITLAYER_RECORDER_MAP)")
    try:
        mapping = load_mapping(args.map)
    except MappingError as e:
        log_to_stderr(f"invalid mapping {args.map}: {e}")
        return 2
    api_key = os.environ.get("COMMITLAYER_API_KEY") or ""
    if not api_key:
        log_to_stderr("COMMITLAYER_API_KEY is not set; running as a passthrough, nothing will be recorded")
    recorder = Recorder(
        mapping,
        api_key=api_key,
        api_url=args.api_url,
        agent_id=args.agent_id,
        workflow_id=args.workflow_id,
        timeout=args.timeout,
        fail_open=not args.no_fail_open,
        warn_unmapped=args.warn_unmapped,
    )
    return run_proxy(command, recorder)


if __name__ == "__main__":
    sys.exit(main())
