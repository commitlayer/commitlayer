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
