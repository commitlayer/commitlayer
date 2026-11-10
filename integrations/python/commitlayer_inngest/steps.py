"""Record an Inngest step as a CommitLayer action, and wait for its verdict as steps.

Nothing here imports ``inngest``: the helpers take the function's ``ctx`` (``inngest.Context`` or
``inngest.ContextSync``: anything with ``.step`` and ``.run_id``) and call ``step.run`` / ``step.sleep`` on it,
so the package imports without the SDK and the tests run against fakes.

Why the action is recorded *inside* the step function: Inngest memoises each step, so a replayed function
invocation returns the stored output without running the step again, and the action is not re-recorded. If a
step is retried before it succeeded, the same ``action_id`` (``inngest:{run_id}:{step_id}``) makes the retries
one action, because the API is idempotent on it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from commitlayer import Client
from commitlayer_integrations.verdict import Verdict, fetch_verification, summarize_verification

__all__ = [
    "CHANNEL",
    "DEFAULT_ACTION_ID",
    "CommitLayerSteps",
    "RecordSpec",
    "build_action_body",
    "recorded_step",
    "recorded_step_async",
    "wait_for_verdict",
    "wait_for_verdict_async",
]

log = logging.getLogger("commitlayer.inngest")

CHANNEL = "inngest"
DEFAULT_ACTION_ID = "inngest:{run_id}:{step_id}"


@dataclass(frozen=True)
class RecordSpec:
    """What to record for one step: the agent's declared intent, taken from the function's own inputs.

    ``action_id`` is a template over ``{run_id}``, ``{step_id}``, ``{subject_id}`` and every param by name.
    """

    contract_id: str
    subject_id: str
    params: Mapping[str, Any] = field(default_factory=dict)
    action_id: str | None = None
    action_type: str | None = None
    subject_kind: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None
    compensates: str | None = None
    contract_version: int | None = None

    @classmethod
    def coerce(cls, value: RecordSpec | Mapping[str, Any]) -> RecordSpec:
        if isinstance(value, RecordSpec):
            return value
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown record keys: {sorted(unknown)}")
        return cls(**dict(value))


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        raise ValueError(f"action_id template: unknown placeholder {{{key}}}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 - Python 3.10


def build_action_body(
    record: RecordSpec | Mapping[str, Any],
    *,
    run_id: str,
    step_id: str,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    requested_at: str | None = None,
) -> dict[str, Any]:
    """The exact ``POST /v1/actions`` body for one step. Pure; asserted as-is in the tests.

    ``workflow_id`` / ``agent_id`` are defaults; values on ``record`` win. ``params`` always carries
    ``inngest_run_id`` so the action can be traced back to the run.
    """
    r = RecordSpec.coerce(record)
    if not r.subject_id:
        raise ValueError("record.subject_id is required")
    params = dict(r.params)
    fields = _Fields(params, run_id=run_id, step_id=step_id, subject_id=str(r.subject_id))
    action_id = (r.action_id or DEFAULT_ACTION_ID).format_map(fields)
    if not action_id or len(action_id) > 120:
        raise ValueError(f"action_id {action_id!r} must be 1-120 characters")
    body: dict[str, Any] = {
        "action_id": action_id,
        "contract_id": r.contract_id,
        "subject_id": str(r.subject_id),
        "requested_at": requested_at or _now(),
        "params": {**params, "inngest_run_id": run_id},
        "channel": CHANNEL,
    }
    optional = (
        ("workflow_id", r.workflow_id or workflow_id),
        ("agent_id", r.agent_id or agent_id),
        ("action_type", r.action_type),
        ("subject_kind", r.subject_kind),
        ("compensates", r.compensates),
        ("contract_version", r.contract_version),
    )
    for key, value in optional:
        if value is not None:
            body[key] = value
    return body


def _split(ctx_or_step: Any, run_id: str | None) -> tuple[Any, str]:
    """``(step, run_id)`` from an Inngest context, or from a bare step plus an explicit ``run_id``."""
    if hasattr(ctx_or_step, "step") and hasattr(ctx_or_step, "run_id"):
        return ctx_or_step.step, run_id or str(ctx_or_step.run_id)
    if run_id is None:
        raise ValueError("pass the Inngest ctx, or a step together with run_id=")
    return ctx_or_step, run_id


def _record(
    client: Client,
    body: dict[str, Any],
    *,
    step_id: str,
    fail_open: bool,
    on_recorded: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any] | None:
    """``POST /v1/actions`` (the SDK's ``_call``, so ``contract_version`` is kept). Fail-open by default."""
    try:
        rec = client._call("POST", "/v1/actions", body)
    except Exception as e:  # noqa: BLE001 - the business step must not depend on the verifier
        if not fail_open:
            raise
        log.warning("commitlayer: action not recorded for step %s: %s", step_id, e)
        return None
    if on_recorded:
        on_recorded(rec)
    return rec


def recorded_step(
    ctx_or_step: Any,
    step_id: str,
    fn: Callable[..., Any],
    *args: Any,
    client: Client,
    record: RecordSpec | Mapping[str, Any],
    agent_id: str | None = None,
    workflow_id: str | None = None,
    fail_open: bool = True,
    on_recorded: Callable[[dict[str, Any]], None] | None = None,
    run_id: str | None = None,
) -> Any:
    """``step.run(step_id, fn, *args)`` for a sync Inngest function, with the action recorded first.

    The record happens inside the step, before ``fn``, so the request time precedes every effect and a
    memoised replay records nothing. ``fn``'s return value is the step's output, unchanged; it is never sent
    to CommitLayer. With ``fail_open=False`` a recording failure raises inside the step (Inngest retries it).
    """
    step, rid = _split(ctx_or_step, run_id)
    spec = RecordSpec.coerce(record)

    def handler() -> Any:
        body = build_action_body(
            spec, run_id=rid, step_id=step_id, agent_id=agent_id, workflow_id=workflow_id
        )
        _record(client, body, step_id=step_id, fail_open=fail_open, on_recorded=on_recorded)
        return fn(*args)

    return step.run(step_id, handler)


async def recorded_step_async(
    ctx_or_step: Any,
    step_id: str,
    fn: Callable[..., Any],
    *args: Any,
    client: Client,
    record: RecordSpec | Mapping[str, Any],
    agent_id: str | None = None,
    workflow_id: str | None = None,
    fail_open: bool = True,
    on_recorded: Callable[[dict[str, Any]], None] | None = None,
    run_id: str | None = None,
) -> Any:
    """:func:`recorded_step` for an async Inngest function; ``fn`` may be sync or async."""
    step, rid = _split(ctx_or_step, run_id)
    spec = RecordSpec.coerce(record)

    async def handler() -> Any:
        body = build_action_body(
            spec, run_id=rid, step_id=step_id, agent_id=agent_id, workflow_id=workflow_id
        )
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _record(client, body, step_id=step_id, fail_open=fail_open, on_recorded=on_recorded),
        )
        out = fn(*args)
        return await out if inspect.isawaitable(out) else out

    return await step.run(step_id, handler)


def _poll_plan(timeout_seconds: float, poll_seconds: float) -> tuple[int, timedelta]:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    return max(1, math.ceil(timeout_seconds / poll_seconds)), timedelta(seconds=poll_seconds)


def wait_for_verdict(
    ctx_or_step: Any,
    action_ref: str,
    *,
    client: Client,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 15.0,
    step_prefix: str | None = None,
) -> Verdict:
    """Wait, as Inngest steps, until the verification of ``action_ref`` settles or the timeout passes.

    Each poll is its own ``step.run`` (memoised, so a replay never re-fetches a settled verdict) followed by a
    ``step.sleep`` of ``poll_seconds`` that suspends the function run instead of holding a worker. The wait is
    ``ceil(timeout_seconds / poll_seconds)`` polls, so the timeout is a lower bound: scheduler latency on each
    resume adds to it. Returns a :class:`~commitlayer_integrations.verdict.Verdict`; ``passed`` is true only
    for a settled PASS under both lenses, ``timed_out`` when the deadline passed while still pending.
    """
    step, _ = _split(ctx_or_step, run_id="")
    prefix = step_prefix or f"commitlayer_verify:{action_ref}"
    polls, pause = _poll_plan(timeout_seconds, poll_seconds)
    verdict: Verdict | None = None
    for i in range(polls):
        verdict = step.run(
            f"{prefix}:poll:{i}",
            lambda: dict(summarize_verification(fetch_verification(client, action_ref, poll_seconds))),
        )
        if verdict["settled"]:
            return verdict
        if i < polls - 1:
            step.sleep(f"{prefix}:sleep:{i}", pause)
    assert verdict is not None
    verdict["timed_out"] = True
    return verdict


async def wait_for_verdict_async(
    ctx_or_step: Any,
    action_ref: str,
    *,
    client: Client,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 15.0,
    step_prefix: str | None = None,
) -> Verdict:
    """:func:`wait_for_verdict` for an async Inngest function."""
    step, _ = _split(ctx_or_step, run_id="")
    prefix = step_prefix or f"commitlayer_verify:{action_ref}"
    polls, pause = _poll_plan(timeout_seconds, poll_seconds)

    async def fetch() -> dict[str, Any]:
        payload = await asyncio.get_running_loop().run_in_executor(
            None, fetch_verification, client, action_ref, poll_seconds
        )
        return dict(summarize_verification(payload))

    verdict: Verdict | None = None
    for i in range(polls):
        verdict = await step.run(f"{prefix}:poll:{i}", fetch)
        if verdict["settled"]:
            return verdict
        if i < polls - 1:
            await step.sleep(f"{prefix}:sleep:{i}", pause)
    assert verdict is not None
    verdict["timed_out"] = True
    return verdict


class CommitLayerSteps:
    """A client plus defaults, bound once per Inngest function.

    ``fn_id`` is the Inngest function id and becomes the default ``workflow_id`` (the SDK's ``ctx`` does not
    carry it, so it is given here). ``run``/``run_async`` wrap :func:`recorded_step`;
    ``wait_for_verdict``/``wait_for_verdict_async`` wrap the verdict wait.
    """

    def __init__(
        self,
        client: Client,
        *,
        fn_id: str | None = None,
        agent_id: str | None = None,
        fail_open: bool = True,
        default_timeout_seconds: float = 900.0,
        poll_seconds: float = 15.0,
        on_recorded: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.fn_id = fn_id
        self.agent_id = agent_id or client.agent_id
        self.fail_open = fail_open
        self.default_timeout_seconds = default_timeout_seconds
        self.poll_seconds = poll_seconds
        self.on_recorded = on_recorded

    def _kw(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "agent_id": self.agent_id,
            "workflow_id": self.fn_id,
            "fail_open": self.fail_open,
            "on_recorded": self.on_recorded,
        }

    def run(self, ctx: Any, step_id: str, fn: Callable[..., Any], *args: Any, **record: Any) -> Any:
        return recorded_step(ctx, step_id, fn, *args, record=record, **self._kw())

    async def run_async(
        self, ctx: Any, step_id: str, fn: Callable[..., Any], *args: Any, **record: Any
    ) -> Any:
        return await recorded_step_async(ctx, step_id, fn, *args, record=record, **self._kw())

    def wait_for_verdict(self, ctx: Any, action_ref: str, *, timeout_seconds: float | None = None) -> Verdict:
        return wait_for_verdict(
            ctx,
            action_ref,
            client=self.client,
            timeout_seconds=self.default_timeout_seconds if timeout_seconds is None else timeout_seconds,
            poll_seconds=self.poll_seconds,
        )

    async def wait_for_verdict_async(
        self, ctx: Any, action_ref: str, *, timeout_seconds: float | None = None
    ) -> Verdict:
        return await wait_for_verdict_async(
            ctx,
            action_ref,
            client=self.client,
            timeout_seconds=self.default_timeout_seconds if timeout_seconds is None else timeout_seconds,
            poll_seconds=self.poll_seconds,
        )
