# commitlayer_temporal

CommitLayer for the Temporal Python SDK. **Temporal makes the workflow durable. CommitLayer independently
verifies the result.**

Two pieces:

- an **activity interceptor** that records a CommitLayer action for each mapped activity *before* the activity
  executes, from the activity's own arguments;
- a **completion verifier**: an activity the workflow calls after its work, which waits for the verification
  to settle and returns the verdict, so the workflow can decide what "done" means.

The boundary, in one sentence: The agent may declare its intent. It may read the verdict. It must never
control the independent facts, the governing contract, or the final evidence.

```bash
pip install 'commitlayer-integrations[temporal]'
export COMMITLAYER_API_KEY=spk_...   # read by commitlayer.Client; never log or print it
```

## 1. Record each activity as an action: the interceptor

```python
from concurrent.futures import ThreadPoolExecutor

from commitlayer import Client
from commitlayer_temporal import CommitLayerInterceptor, CommitLayerVerifier
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker

cl = Client()  # COMMITLAYER_API_KEY / COMMITLAYER_API_URL from the environment

MAPPING = {
    # activity name -> what to record
    "refund_payment": {
        "contract_id": "refund_workflow",
        "subject": "kwargs.order_id",  # a path into the activity's arguments
        "params": {"amount": "kwargs.amount", "reason": "kwargs.reason"},
        "subject_kind": "order",
        "action_type": "refund_order",
        # "action_id": "refund:{subject_id}",               # optional template; see below
        # "contract_version": 2,                            # optional: pin the version
    },
    "reverse_refund": {
        "contract_id": "refund_reversal",
        "subject": "kwargs.order_id",
        "compensates": "kwargs.failed_action_id",  # path to the action this one corrects
    },
}


async def main() -> None:
    temporal = await TemporalClient.connect("localhost:7233")
    verifier = CommitLayerVerifier(cl)
    worker = Worker(
        temporal,
        task_queue="orders",
        workflows=[RefundWorkflow],
        activities=[refund_payment, reverse_refund, notify_customer, verifier.commitlayer_verify],
        activity_executor=ThreadPoolExecutor(8),  # the verifier is a sync activity
        interceptors=[CommitLayerInterceptor(cl, MAPPING, agent_id="refund-agent")],
    )
    await worker.run()
```

What gets recorded for `refund_payment("order_10231", 180.0, "customer_cancelled")` in workflow `wf-42`,
activity id `7`, attempt `2`:

```json
{
  "action_id": "temporal:wf-42:7",
  "contract_id": "refund_workflow",
  "workflow_id": "wf-42",
  "agent_id": "refund-agent",
  "action_type": "refund_order",
  "subject_id": "order_10231",
  "subject_kind": "order",
  "requested_at": "2026-10-15T10:42:00Z",
  "params": {"amount": 180.0, "reason": "customer_cancelled", "temporal_activity_id": "7", "temporal_attempt": 2},
  "channel": "temporal"
}
```

That is the body of `POST /v1/actions`, nothing more. The activity's return value is never sent: verification
reads the systems of record (payment provider, order system, CRM), not the agent's report of its own work.

### Mapping reference

| Key | Meaning |
|---|---|
| `contract_id` | The action contract (required). |
| `subject` | Path to the subject id (required). |
| `params` | `{name: path}`: the parameters the contract limits (`amount`, `tier`). |
| `action_id` | Template; default `temporal:{workflow_id}:{activity_id}`. |
| `action_type`, `subject_kind` | Sent as given. |
| `compensates` | Path to the `action_id` of an earlier, failed action this one corrects. |
| `contract_version` | Pin the contract version the action is recorded under. |

**Paths**: `args.0.order_id` (first positional argument, then key or attribute), `kwargs.order_id` (the
argument bound to the activity function's parameter `order_id`; Temporal passes arguments positionally, the
integration binds them to the signature), or just `order_id` (same as `kwargs.order_id`). Dicts, dataclasses
and plain objects all work. A path that does not resolve is a recording failure (see fail-open), never a
silent zero.

**Action id**: the default `temporal:{workflow_id}:{activity_id}` is the same for every retry attempt of one
activity execution, and the API is idempotent on `action_id`, so retries update one action instead of recording
several. Templates may use `{workflow_id}`, `{workflow_run_id}`, `{activity_id}`, `{attempt}`, `{activity}`,
`{subject_id}` and any param by name (`{amount}`). The activity id and attempt are always in `params` as
`temporal_activity_id` / `temporal_attempt`.

**Defaults**: `agent_id` (falls back to the client's) and `workflow_id` (default: the Temporal workflow id)
are set on the interceptor. `channel` is always `temporal`. `requested_at` is stamped when the interceptor
runs, before the activity, so the request time precedes every effect.

### Fail-open

With `fail_open=True` (default), a recording failure (API unreachable, 5xx, a mapping path that does not
resolve) is logged as a warning on the `commitlayer.temporal` logger and the activity runs anyway. The gap shows
up on the CommitLayer side as an action that was never recorded, not as a business action that never
happened. With `fail_open=False` the interceptor raises `ApplicationError(type="CommitLayerRecordFailed",
non_retryable=True)` *before* the activity runs, so the activity fails without retrying and the workflow sees
the failure. Nothing in the log line carries the API key.

## 2. Gate completion on the verdict: the verifier

Recording does not verify, and a verification is provisional until its settlement window closes and every
source has caught up (`status: "settled"`). The verifier is a sync activity, `commitlayer_verify(action_ref,
timeout_seconds=900, poll_seconds=5)`, that waits on the API's completion gate (`GET /v1/actions/{ref}/wait`,
held for up to `poll_seconds` per call and answered the moment the verdict settles), heartbeating after each
call, until the verification is settled or the timeout passes. The loop lives in
`commitlayer_integrations.verdict.poll_until_settled`; the SDK's own `wait_for_settlement` does the same outside a
workflow engine.

It returns a small JSON-serialisable dict, safe for Temporal history:

```python
{
    "action_id": "temporal:wf-42:7",
    "contract_id": "refund_workflow",
    "contract_version": 2,
    "status": "settled",  # "pending" | "settled" | "unverified"
    "settled": True,
    "outcome": "PASS",  # did every expected effect happen?
    "policy": "PASS",  # ...and were preconditions, limits and prohibitions respected?
    "passed": True,  # settled AND both lenses PASS: the completion signal
    "reason_codes": {"outcome": ["effect_observed:stripe.refund_created", ...], "policy": [...]},
    "reason_labels": {...},
    "headline": "PASS — all 4 expected effects observed within 15m; ...",
    "settles_at": "2026-10-15T10:57:00Z",
    "verdict_hash": "...",
    "timed_out": False,  # True when the timeout passed while still pending
}
```

### The pattern

```python
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from commitlayer_temporal import CommitLayerVerifier


@workflow.defn
class RefundWorkflow:
    @workflow.run
    async def run(self, order_id: str, amount: float, reason: str) -> dict:
        handle = workflow.start_activity(
            refund_payment,
            args=[order_id, amount, reason],
            activity_id="refund",  # fixed id: the action is temporal:<wf>:refund
            start_to_close_timeout=timedelta(minutes=2),
        )
        await handle

        action_ref = f"temporal:{workflow.info().workflow_id}:refund"
        verdict = await workflow.execute_activity(
            CommitLayerVerifier.commitlayer_verify,
            args=[action_ref, 900],  # wait up to 15 minutes for settlement
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(minutes=1),
        )

        if verdict["passed"]:
            # Only now: the systems of record show the refund, the cancellation, the note and the message.
            await workflow.execute_activity(
                notify_customer, args=[order_id, "done"], start_to_close_timeout=timedelta(minutes=1)
            )
            return {"status": "completed", "verdict": verdict}

        if verdict["settled"]:
            # FAIL (or UNKNOWN) under a lens: route to compensation. The compensating activity's action
            # carries compensates=<action_ref>, so the failed action is marked COMPENSATED once the
            # correction itself settles PASS. The original verdict never changes.
            await workflow.execute_activity(
                reverse_refund, args=[order_id, action_ref], start_to_close_timeout=timedelta(minutes=2)
            )
            return {"status": "compensated", "verdict": verdict}

        # timed_out while pending (UNKNOWN past the deadline): the workflow's decision. Wait again, escalate
        # to a person, or hold the confirmation. Do not report success.
        return {"status": "unresolved", "verdict": verdict}
```

Three rules this pattern enforces:

1. **No "done" before PASS.** The confirmation to the customer runs only on `verdict["passed"]`, which is a
   settled PASS under both lenses. A provisional PASS is not a completion signal.
2. **FAIL routes to compensation.** The compensation is itself a recorded action (`compensates` in the mapping),
   so it is verified on the same terms; nothing is marked resolved on anyone's say-so.
3. **UNKNOWN past the timeout is the workflow's decision.** The verifier reports `timed_out: True` with the
   last status it saw; it does not guess. A workflow may wait longer (call the activity again: polls are
   idempotent reads), escalate, or hold.

Why a fixed `activity_id`: the default action id is built from the Temporal activity id, so a workflow that
sets `activity_id="refund"` knows the action reference without passing anything around. Without it, use an
`action_id` template over the arguments (`"refund:{subject_id}"`) and build the same string in the workflow.

The verifier activity is synchronous because the SDK's transport is `urllib`; register an `activity_executor`
on the worker. It heartbeats after every poll with `{status, outcome, policy}`. Cancellation reaches it as
Temporal's `CancelledError` in its thread; between polls it also checks `activity.is_cancelled()`.

## Testing without a server

The interceptor's body-building is a pure function, `commitlayer_temporal.build_action_body(mapping, ref=...,
args=..., kwargs=...)`, and `temporalio.testing.ActivityEnvironment` runs both the interceptor and the verifier
with `activity.info()` and heartbeats in place. See `integrations/python/tests/test_temporal_*.py`: a fake
transport on `commitlayer.Client(transport=...)` captures the exact request bodies.
