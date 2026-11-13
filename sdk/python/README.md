# commitlayer (Python)

Record agent actions, push facts from systems of record, read verdicts and draft contracts. Standard library
only; Python 3.10+.

```bash
pip install commitlayer
export COMMITLAYER_API_KEY=spk_...          # the agent runtime's key: a *recorder* key (records actions, nothing else)
export COMMITLAYER_EVIDENCE_KEY=spk_...     # the pipeline's key: an *evidence* key bound to the sources it pushes for
# COMMITLAYER_API_URL defaults to https://api.commitlayer.ai. One credential per side of the line: the key that
# records an action can never also supply the facts that verify it.
```

## Record an action where it happens

```python
from commitlayer import Client

cl = Client(agent_id="support-bot")


@cl.action(
    "refund_workflow",
    subject=lambda order_id, **_: order_id,
    params=lambda amount, **_: {"amount": amount},
    subject_kind="order",
    action_id=lambda order_id, **_: f"refund:{order_id}",  # retries update one claim instead of recording two
)
def refund(order_id: str, amount: float) -> dict:
    return payments.refund(order_id, amount)  # the agent's own work, unchanged


refund("order_10231", 180.0)
print(refund.last_recorded["verification"]["summary"]["headline"])
```

The action is recorded **before** the function runs, so its request time precedes every effect. The function's
return value is never changed and never consulted: verification reads the systems of record. If the API cannot
be reached the function still runs and a warning is logged (`fail_open=True`); pass `fail_open=False` to raise.

## Push facts, with a watermark

Use an **evidence** key here, bound to the sources it pushes for (Settings → API keys). The key your agent runtime records actions with is a **recorder** key and cannot push facts: the credential that records an action never also supplies the facts that verify it.

```python
import os

evidence = Client(api_key=os.environ["COMMITLAYER_EVIDENCE_KEY"])  # the pipeline's own client, not the runtime's

evidence.push_facts(
    [
        {
            "source": "crm",
            "subject_id": "order_10231",
            "fact_type": "note_added",
            "occurred_at": "2026-10-13T10:42:09Z",
        }
    ],
    watermarks={"crm": "2026-10-13T10:45:00Z"},  # everything up to here has been sent for crm
)
```

A verdict that is not yet a PASS settles only when its window has closed **and** every source it reads has a
watermark past the end of the window. Declare one when your feed lags; a source that never declares one is taken
as current.

## Draft a contract from what was recorded

```python
d = cl.draft_contract("refund_order", lookback_days=30)
print(d["yaml"])  # expected effects, window, preconditions, limits, from the sample
print(d["stats"]["effects"])
v = cl.validate_contract(d["yaml"])  # errors, and weakening warnings against the version it would replace
cl.save_contract(d["yaml"])  # the response carries the same warnings
```

Every value in a draft is what the sample showed, not what the policy says. Review it before saving.

## Read a verdict

```python
v = cl.get_action("refund:order_10231")
v["status"]  # pending | settled
v["settlement"]["waiting_on"]  # sources still behind the window, if any
v["reversal"]  # the later record that undid the action, if any
v["sources"]  # trust level and watermark per source the contract read
```

## Wait for the verdict before saying "done"

```python
v = cl.wait_for_settlement("refund:order_10231", timeout=300)  # blocks until settled or 5 minutes
if v["timed_out"]:
    ...  # still pending: your policy, never "success"
elif v["lenses"]["outcome"]["verdict"] == "PASS":
    notify_customer("Your order is cancelled and refunded.")
else:
    open_recovery(v)  # FAIL: the reasons name what is missing or forbidden
```

The API holds each call for up to 60 seconds; the SDK loops. The gate reads evidence; it never changes a verdict.

## Install a workflow pack (drafts, never activation)

```python
packs = cl.packs.list()["items"]                                   # Stripe Customer Operations, x402, Coinbase
detail = cl.packs.get("stripe_customer_operations")                # contracts, requirements, templates, versions
effective = cl.packs.configure("stripe_customer_operations", {"refund_order": {"approved_limit": 100}})
report = cl.packs.dry_run("stripe_customer_operations", {"refund_order": {"approved_limit": 100}})
assert report["summary"]["ok"] == report["summary"]["total"]      # every fixture case holds under your limits
out = cl.packs.install("stripe_customer_operations", {"refund_order": {"approved_limit": 100}}, confirm=True)
print(out["activation"]["how"])                                    # POST /v1/contracts/<id>/select, by a person
mapping = cl.packs.recorder_mapping("stripe_customer_operations") # contract versions pinned
```

`configure` and `dry_run` write nothing. `install` saves the configured contracts as **drafts**: nothing governs a
live action until a person activates a version in the app or with `POST /v1/contracts/{id}/select`. Below the
pack's minimum plan the service answers `402 plan_required` with `upgrade_to`.

## What is retried

When the API cannot admit a request it answers `503 busy` (the service is at its limit) or `429` (`workspace_busy`:
this workspace already has its share of writes in progress; `rate_limited`: its requests a minute are spent) with a
`Retry-After` header. The client waits the time the header states and sends the same request again, so a burst
that the API spreads over a few seconds never reaches your code as an error. Since 0.1.3.

| | |
|---|---|
| Retried | An answer of **429 or 503 that carries `Retry-After`**, as seconds (`1`, `1.5`) or an HTTP-date, for any method of the client. |
| Not retried | Every other status (400, 402, 404, 409, 500 and the rest), a 429 or 503 **without** the header (the API's limit on requests in flight answers a plain `503` like that), a timeout and a network error: after those the client cannot know whether the request arrived. |
| How often | `max_retries=3` times after the first attempt. `max_retries=0` turns retrying off. |
| How long | Never less than the header states, plus a random addition of up to twice as much, so the calls of a burst that were refused together come back spread out, and never more than `max_retry_wait`. A stated wait above `max_retry_wait=10.0` seconds is not waited at all: the error is raised at once (the Developer plan's daily cap, `429 fair_use_exceeded`, states the seconds to midnight). |
| Worst case | One call waits at most `max_retries * max_retry_wait` seconds, 30 by default, on top of its requests. The API states one second today, so each retry waits between one and three seconds: a call refused once adds one to three seconds, a call that uses all three retries adds three to nine. |
| The request | Identical on every attempt, byte for byte. `record_action` chooses the `action_id` before the first attempt, so every attempt names the same action and the API records it once. |

```python
from commitlayer import Client, CommitLayerError

cl = Client(max_retries=3, max_retry_wait=10.0)   # the defaults
single = Client(max_retries=0)                    # never retry: the first refusal is raised

try:
    cl.record_action("refund_workflow", "order_10231", action_id="refund:order_10231")
except CommitLayerError as e:
    e.status, e.code            # 429, "workspace_busy": the last answer, as before
    e.retry_after               # seconds the last answer asked to wait, or None when it stated none
    e.attempts                  # how many times the request was sent (4 once three retries are used up)
```

One request is the exception. `POST /v1/actions` is idempotent on `action_id`, and the API may answer `503 busy`
after the action itself is stored, so a recording that names no `action_id` is sent once and never repeated.
`record_action` and the `@cl.action` decorator always name one; this only concerns a body you build yourself for
`cl.request("POST", "/v1/actions", ...)` or an inline `cl.verify(action=...)` that is stored.

For the same reason a `503` that `record_action` still raises, after its retries or without any, does not prove
the action is absent: the API may have stored it and failed only to verify it in time. Recording it again with the
same `action_id` is safe, and `cl.get_action(action_id)` says whether it is there.

Every attempt is a request to the API, and today a write the API refuses as `workspace_busy` or `busy` has already
been counted against the plan's write requests a minute (60 on the Developer plan). A large burst that is mostly
refused and retried can therefore spend that minute's budget and end in `429 rate_limited`. Retries make a burst
succeed, not go faster: a caller that sends its writes a few at a time is seldom refused in the first place.

The wait blocks the calling thread (`time.sleep`), the decorator included, and a retried poll can add up to the
same bound to `wait_for_settlement(timeout=...)`. In an event loop, call the client from a worker thread
(`await asyncio.to_thread(cl.record_action, ...)`) or pass `max_retries=0` and schedule the retry yourself from
`e.retry_after`. Each retry writes one line at INFO level to the `commitlayer` logger: method, path, status, code and
the wait, never the key or the body.

A custom `transport` is a function `(method, path, body)` that returns `(status, payload)` or
`(status, payload, headers)`. Only the second form can be retried: without response headers there is no stated
wait, so the first refusal is raised as it always was. `headers` is a dict or another mapping, the headers object
of `urllib` and `http.client` (`response.headers`), or a list of `(name, value)` pairs; the name is matched in any
case, and a header that is there twice counts at the longer wait.

## Correct a failed action

```python
cl.record_action(
    "refund_workflow",
    "order_10231",
    params={"amount": 180.0},
    action_id="refund:order_10231:fix",
    compensates="refund:order_10231",
)
```

Once the correction settles PASS, the failed action carries `resolution.state == "compensated"`; its own verdict never changes.
