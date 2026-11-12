# commitlayer (JavaScript / TypeScript)

Record agent actions, push facts from systems of record, read verdicts and draft contracts. Zero runtime
dependencies; Node 20+ (uses the global `fetch` and `crypto`). Ships with TypeScript types.

```bash
npm install commitlayer
export COMMITLAYER_API_KEY=spk_...          # the agent runtime's key: a *recorder* key (records actions, nothing else)
export COMMITLAYER_EVIDENCE_KEY=spk_...     # the pipeline's key: an *evidence* key bound to the sources it pushes for
# COMMITLAYER_API_URL defaults to https://api.commitlayer.ai. One credential per side of the line: the key that
# records an action can never also supply the facts that verify it.
```

## Record an action where it happens

```js
import { Client } from "commitlayer";

const cl = new Client({ agentId: "support-bot" });

const refund = cl.action("refund_workflow", {
  subject: (orderId) => orderId,
  params: (orderId, amount) => ({ amount }),
  subjectKind: "order",
  actionId: (orderId) => `refund:${orderId}`, // retries update one claim instead of recording two
})(async (orderId, amount) => {
  return payments.refund(orderId, amount); // the agent's own work, unchanged
});

await refund("order_10231", 180.0);
console.log(refund.lastRecorded.verification.summary.headline);
```

The action is recorded **before** the function runs, so its request time precedes every effect. The function's
return value is never changed and never consulted: verification reads the systems of record. If the API cannot
be reached the function still runs and a warning is logged with `console.warn` (`failOpen: true`); pass
`failOpen: false` to throw instead.

The wrapper always returns a Promise, also for a synchronous function, because the record is awaited first.
`subject` is a function over the call's arguments or the 0-based index of the argument holding the subject id.
`workflowId` and `compensates` take a string or a function over the arguments; `compensates` names the earlier
action this one corrects.

Without the wrapper:

```js
const rec = await cl.recordAction("refund_workflow", "order_10231", {
  params: { amount: 180.0 },
  actionType: "refund_order",
  subjectKind: "order",
  actionId: "refund:order_10231",
});
```

## Push facts, with a watermark

Use an **evidence** key here, bound to the sources it pushes for (Settings → API keys). The key your agent runtime records actions with is a **recorder** key and cannot push facts: the credential that records an action never also supplies the facts that verify it.

```js
const evidence = new Client({ apiKey: process.env.COMMITLAYER_EVIDENCE_KEY }); // the pipeline's own client

await evidence.pushFacts(
  [
    {
      source: "crm",
      subject_id: "order_10231",
      fact_type: "note_added",
      occurred_at: "2026-10-13T10:42:09Z",
    },
  ],
  { watermarks: { crm: "2026-10-13T10:45:00Z" } }, // everything up to here has been sent for crm
);
```

A verdict that is not yet a PASS settles only when its window has closed **and** every source it reads has a
watermark past the end of the window. Declare one when your feed lags; a source that never declares one is taken
as current.

## Draft a contract from what was recorded

```js
const d = await cl.draftContract("refund_order", { lookbackDays: 30 });
console.log(d.yaml); // expected effects, window, preconditions, limits, from the sample
console.log(d.stats.effects);
const v = await cl.validateContract(d.yaml); // errors, and weakening warnings against the version it would replace
await cl.saveContract(d.yaml); // the response carries the same warnings
```

Every value in a draft is what the sample showed, not what the policy says. Review it before saving.

## Read a verdict

```js
const v = await cl.getAction("refund:order_10231");
v.status; // pending | settled
v.settlement.waiting_on; // sources still behind the window, if any
v.reversal; // the later record that undid the action, if any
v.sources; // trust level and watermark per source the contract read
```

## Wait for the verdict before saying "done"

```js
const v = await cl.waitForSettlement("refund:order_10231", { timeoutMs: 300_000 });
if (v.timed_out) {
  // still pending: your policy, never "success"
} else if (v.lenses.outcome.verdict === "PASS") {
  await notifyCustomer("Your order is cancelled and refunded.");
} else {
  await openRecovery(v); // FAIL: the reasons name what is missing or forbidden
}
```

The API holds each call for up to 60 seconds; the SDK loops. The gate reads evidence; it never changes a verdict.

## Install a workflow pack (drafts, never activation)

```ts
const { items } = await cl.packs.list();
const detail = await cl.packs.get("x402_service_delivery");
const effective = await cl.packs.configure("x402_service_delivery", { purchase_api_call: { approved_limit: 2 } });
const report = await cl.packs.dryRun("x402_service_delivery", { purchase_api_call: { approved_limit: 2 } });
const out = await cl.packs.install("x402_service_delivery", { purchase_api_call: { approved_limit: 2 } }, { confirm: true });
console.log(out.activation);                        // the person's step: POST /v1/contracts/{id}/select
const mapping = await cl.packs.recorderMapping("x402_service_delivery");
```

`configure` and `dryRun` write nothing; `install` saves the configured contracts as **drafts** and never
activates. Below the pack's minimum plan the service answers `402 plan_required` with `upgrade_to`.

## Errors

Every method throws a `CommitLayerError` for a 4xx or 5xx response, with `status`, `code`, `message` and
`details` taken from the API's `{ error: { code, message, details } }` body.

```js
import { CommitLayerError } from "commitlayer";

try {
  await cl.getAction("missing");
} catch (e) {
  if (e instanceof CommitLayerError && e.code === "not_found") {
    /* ... */
  }
}
```

## TypeScript

```ts
import { Client, type Json } from "commitlayer";

const cl = new Client({ apiKey: "spk_..." });

async function refundOrder(orderId: string, amount: number): Promise<{ refundId: string }> {
  return payments.refund(orderId, amount);
}

// The wrapped function keeps refundOrder's parameters; its result is Promise<{ refundId: string }>.
const refund = cl.action<typeof refundOrder>("refund_workflow", {
  subject: (orderId) => orderId,
  params: (_orderId, amount) => ({ amount }),
  onRecorded: (rec: Json) => console.log(rec.verification),
})(refundOrder);

const { refundId } = await refund("order_10231", 180);
```

For tests, inject a transport instead of a network: `new Client({ transport: async (method, path, body) => ({ status: 201, json: {} }) })`.

## Develop

```bash
cd sdk/js
npm install   # typescript and @types/node only
npm run build # compiles src/ to dist/
npm test      # compiles the node:test suite and runs it offline
```
