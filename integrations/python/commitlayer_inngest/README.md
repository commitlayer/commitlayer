# commitlayer_inngest

CommitLayer for the Inngest Python SDK. The same positioning as the Temporal package, in Inngest's terms:
**Temporal makes the workflow durable. CommitLayer independently verifies the result.** Here Inngest makes the
function durable; CommitLayer still verifies the result independently, from the systems of record.

Two pieces:

- `recorded_step(ctx, "name", fn, *args, client=..., record={...})`: a wrapper for `step.run` that records a
  CommitLayer action *inside* the step, before `fn` runs;
- `wait_for_verdict(ctx, action_ref, client=..., timeout_seconds=...)`: the completion gate, built as steps
  (`step.run` to poll, `step.sleep` between polls), returning the verdict once it is settled.

The boundary, in one sentence: The agent may declare its intent. It may read the verdict. It must never
control the independent facts, the governing contract, or the final evidence.

```bash
pip install 'commitlayer-integrations[inngest]'
export COMMITLAYER_API_KEY=spk_...   # read by commitlayer.Client; never log or print it
```

The helpers are duck-typed over Inngest's `ctx` (`.step`, `.run_id`), so the package itself has no import of
`inngest`; the extra installs the SDK for your function code.

## 1. Record a step as an action

```python
import inngest
from commitlayer import Client
from commitlayer_inngest import CommitLayerSteps

inngest_client = inngest.Inngest(app_id="orders")
cl = CommitLayerSteps(Client(), fn_id="order-refund", agent_id="refund-agent")  # fn_id -> workflow_id


@inngest_client.create_function(
    fn_id="order-refund",
    trigger=inngest.TriggerEvent(event="orders/refund.requested"),
)
async def order_refund(ctx: inngest.Context) -> dict:
    order_id = ctx.event.data["order_id"]
    amount = ctx.event.data["amount"]

    refund = await cl.run_async(
        ctx,
        "refund",  # step id
        payments.refund,
        order_id,
        amount,  # the agent's own work, unchanged
        contract_id="refund_workflow",
        subject_id=order_id,
        subject_kind="order",
        action_type="refund_order",
        params={"amount": amount, "reason": ctx.event.data.get("reason")},
    )

    verdict = await cl.wait_for_verdict_async(ctx, f"inngest:{ctx.run_id}:refund", timeout_seconds=900)

    if verdict["passed"]:
        await ctx.step.run("notify", messaging.notify, order_id, "done")  # only now
        return {"status": "completed", "verdict": verdict}
    if verdict["settled"]:
        await cl.run_async(
            ctx,
            "reverse",
            payments.reverse_refund,
            order_id,
            contract_id="refund_reversal",
            subject_id=order_id,
            compensates=f"inngest:{ctx.run_id}:refund",  # the failed action, marked COMPENSATED on PASS
        )
        return {"status": "compensated", "verdict": verdict}
    return {"status": "unresolved", "verdict": verdict}  # UNKNOWN past the timeout: your decision
```

Sync functions (`inngest.ContextSync`) use `cl.run(...)` and `cl.wait_for_verdict(...)`. The module-level
functions `recorded_step`, `recorded_step_async`, `wait_for_verdict` and `wait_for_verdict_async` take the
client and defaults explicitly; `CommitLayerSteps` just binds them once.

What gets recorded for the `refund` step of run `01J...X`:

```json
{
  "action_id": "inngest:01J...X:refund",
  "contract_id": "refund_workflow",
  "workflow_id": "order-refund",
  "agent_id": "refund-agent",
  "action_type": "refund_order",
  "subject_id": "order_10231",
  "subject_kind": "order",
  "requested_at": "2026-10-15T10:42:00Z",
  "params": {"amount": 180.0, "reason": "customer_cancelled", "inngest_run_id": "01J...X"},
  "channel": "inngest"
}
```

That is the body of `POST /v1/actions`. The step's return value is Inngest's memoised output and is never sent
to CommitLayer: verification reads the systems of record, not the agent's report of its own work.

### Record reference (`record={...}` or keyword arguments on `CommitLayerSteps.run`)

| Key | Meaning |
|---|---|
| `contract_id`, `subject_id` | Required. |
| `params` | The parameters the contract limits (`amount`, `tier`). `inngest_run_id` is always added. |
| `action_id` | Template over `{run_id}`, `{step_id}`, `{subject_id}` and params by name; default `inngest:{run_id}:{step_id}`. |
| `action_type`, `subject_kind` | Sent as given. |
| `workflow_id`, `agent_id` | Override the binder's `fn_id` / `agent_id`. |
| `compensates` | The `action_id` of an earlier, failed action this one corrects. |
| `contract_version` | Pin the contract version the action is recorded under. |

### Why the record happens inside the step

Inngest memoises steps: when a function run is replayed (after a sleep, a retry of a later step, a redeploy),
a step that already succeeded returns its stored output and its handler does not run. Recording inside the
handler means a replay records nothing. If the step itself is retried before it succeeded, every attempt
sends the same `action_id` (`inngest:{run_id}:{step_id}`), and the API is idempotent on it, so the retries are
one action with an updated `requested_at`.

### Fail-open

With `fail_open=True` (default), a recording failure is logged as a warning on the `commitlayer.inngest`
logger and `fn` runs anyway; the gap shows on the CommitLayer side as an action never recorded. With
`fail_open=False` the failure raises inside the step before `fn` runs, so Inngest retries the step and `fn`
has not executed. Nothing in the log line carries the API key.

## 2. Gate completion on the verdict

Recording does not verify, and a verification stays `pending` until its settlement window closes and every
source has caught up. `wait_for_verdict` polls `GET /v1/actions/{ref}` as a sequence of steps:

```
commitlayer_verify:<ref>:poll:0   step.run   -> verdict (memoised)
commitlayer_verify:<ref>:sleep:0  step.sleep -> poll_seconds, the run is suspended, no worker held
commitlayer_verify:<ref>:poll:1   ...
```

until the verification is settled or `ceil(timeout_seconds / poll_seconds)` polls have run. Each poll goes
through the API's completion gate (`GET /v1/actions/{ref}/wait`, held for up to `poll_seconds` and answered the
moment the verdict settles), so a verdict is rarely later than one poll interval. Each poll is its
own memoised step, so a replay never re-fetches a verdict it already saw. The timeout is a lower bound:
scheduler latency on each resume adds to it. Both `inngest.Step` and `inngest.StepSync` provide `sleep`.

It returns the same verdict dict as the Temporal verifier (JSON, safe as step output):

```python
{
    "action_id": ...,
    "status": "settled",
    "settled": True,
    "outcome": "PASS",
    "policy": "PASS",
    "passed": True,
    "reason_codes": {"outcome": [...], "policy": [...]},
    "reason_labels": {...},
    "headline": "...",
    "settles_at": ...,
    "verdict_hash": ...,
    "timed_out": False,
}
```

Three rules the pattern above enforces:

1. **No "done" before PASS.** The customer notification runs only on `verdict["passed"]`: a settled PASS under
   both lenses (outcome: every expected effect happened; policy: and no precondition, limit or prohibition was
   violated). A provisional PASS is not a completion signal.
2. **FAIL routes to compensation.** The compensating step is itself a recorded action with `compensates`, so it
   is verified on the same terms; the failed action is marked COMPENSATED only when the correction settles
   PASS, and its own verdict never changes.
3. **UNKNOWN past the timeout is the function's decision.** `timed_out: True` carries the last status seen;
   the helper does not guess. Wait again (`wait_for_verdict` with a different `step_prefix`), escalate, or
   hold the confirmation. Do not report success.

## Testing without a dev server

`build_action_body(record, run_id=..., step_id=...)` is pure and returns the exact request body; the step
helpers only need an object with `run(step_id, handler)` and `sleep(step_id, duration)`. See
`integrations/python/tests/test_inngest.py`: fake steps that memoise like Inngest, a fake transport on
`commitlayer.Client(transport=...)`.
