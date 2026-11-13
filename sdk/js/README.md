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
`details` taken from the API's `{ error: { code, message, details } }` body. Two more fields say what happened
on the way: `retryAfterMs`, the wait the server stated in `Retry-After` in milliseconds (`undefined` when the
answer carried none), and `attempts`, how many times the request was sent (see [Retries](#retries)).

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

## Retries

The API refuses a request it cannot take right now with an answer made to be retried: `503 busy` when the write
path or the database is full, `429 workspace_busy` when one workspace already has its share of the write path in
progress, `429 rate_limited` past the plan's requests a minute. Each carries a `Retry-After` header. The
client waits the stated time and sends the same request again, so a burst of a few dozen writes is a short wait,
not an error.

- **Retried:** an answer with status `429` or `503` **and** a `Retry-After` header the client can read: a number
  of seconds, whole or decimal, or an HTTP-date (`Wed, 21 Oct 2026 07:28:00 GMT`). On every method, reads included.
- **Not retried:** any other status (`400`, `401`, `402`, `404`, `409`, `500`, `502`, `504`, ...); a `429` or `503`
  without `Retry-After`, or with one that is not a wait (`soon`, `-1`); a network error or a timeout, because after
  those the client cannot know whether the request was applied.
- **The wait:** the time the server stated, never less, plus a random addition of up to twice as much, so that
  requests refused together come back spread out and are not refused together again: one stated second is a wait
  of one to three. Never more than `maxRetryWaitMs` (default `10000`); close to it the addition is a share of
  what is left under it. A stated wait above `maxRetryWaitMs` is not waited for at all: the error is thrown at
  once. That is what happens to `429 fair_use_exceeded`, which states the seconds until midnight UTC.
- **How many:** `maxRetries` times after the first attempt, default `3`. **`maxRetries: 0` turns retrying off.**
- **Worst case:** the waits add at most `maxRetries × maxRetryWaitMs`, 30 seconds with the defaults. The API's own
  busy answers state one second, so a call refused once waits one to three seconds and a call that uses all
  three retries waits three to nine. `timeoutMs` (default 20 seconds) applies to each attempt and the wait is not
  counted against it, so the longest a call can take is
  `(maxRetries + 1) × timeoutMs + maxRetries × maxRetryWaitMs`: 110 seconds with the defaults. That is an API
  that answers slowly and then refuses, every time: the retries multiply the time to a failure, and a smaller
  `maxRetries` or `timeoutMs` bounds it.
- **The same request every time:** each attempt sends the same body, fixed when the call starts. The `action_id`
  the client generates when you give none is generated once, before the first attempt, `requested_at` is set
  once, and a retry sends `params`, the facts or an inline action as they were at that moment, whatever your
  code has done to those objects during the wait. The API records one action however many attempts it took.

```js
const cl = new Client({
  maxRetries: 2, // default 3; 0 is off
  maxRetryWaitMs: 2_000, // default 10000
  onRetry: (r) => console.warn(`commitlayer: ${r.status} ${r.code} on ${r.method} ${r.path}, retry ${r.attempt} of ${r.maxRetries} in ${r.waitMs} ms`),
});
```

Nothing is logged unless you pass `onRetry`; it is called once per retry, before the wait, and never receives
the API key or the request body. When the retries are used up the error is the **last** answer's
`CommitLayerError`, with `retryAfterMs` (what the server asked for) and `attempts` (how many times the request
was sent).

**How large a burst.** The API works on a few writes of one workspace at a time, lets as many wait and refuses
the rest at once. Measured against an API set to work on three writes of a workspace at a time, with the client's
defaults, 100 `recordAction` calls started at the same instant from one workspace were all recorded, within about
seven seconds and mostly on the second attempt; with `maxRetries: 0` most of a burst of 30 was refused. Three
retries are sized for that, not for any burst. When a write took several times longer (a contract that reads
more), 60 at once were still all recorded and 100 at once left a few calls with `429 workspace_busy` after about
ten seconds. For bursts of that size send the writes a few at a time, or raise `maxRetries`. Retries make a burst
succeed, not go faster.

Two answers still reach your code as errors:

- **`429 rate_limited` past the plan's writes a minute.** It is retried like the others, but the wait it states
  is for one request. A burst larger than the plan allows in a minute cannot fit into a few seconds of waiting,
  and the calls beyond the allowance end in `429 rate_limited` once their retries are used up. Every attempt is
  a request to the API and counts against the plan's requests a minute, so a large burst that is mostly refused
  and retried spends that budget as well.
- **A plain `503` without `Retry-After`.** With more than about 64 requests in flight to one of its processes the
  API answers `503 Service Unavailable` as plain text, with no header to wait for. The client reports it with
  code `http_error` and does not retry it, so a client that opens a hundred connections at once can see most of
  its calls fail at once. Bound the concurrency in your code.

One request is the exception. `POST /v1/actions` is idempotent on `action_id`, and the API may answer `503 busy`
after the action itself is stored, so a recording that names no `action_id` is sent once and never repeated: it
throws on the first refusal, a `429` included. `recordAction` and the `action` wrapper always name one; this only
concerns a body you build yourself for `cl.request("POST", "/v1/actions", ...)` or an inline
`cl.verify({ action })` that is stored. For the same reason a `503 busy` that `recordAction` still throws, after
its retries or without any, does not prove the action is absent. `cl.getAction(actionId)` says whether it is
there, and recording it again under the same `action_id` never makes a second action: the API returns the stored
one, or answers `409 conflict` when the new request differs from it, for instance in a `requestedAt` you did not
pass again.

The `action` wrapper awaits the record before it runs your function, so a retried record delays the function by
the waits above. Where a few seconds matter more than the record, give the client a smaller `maxRetries` and
`maxRetryWaitMs`. A retried poll can add the same waits to `waitForSettlement({ timeoutMs })`.

The wait is an ordinary timer and a call cannot be cancelled once it has started. A call you do not await
therefore keeps a Node process alive until it settles, waits included: a script that fires a record and ends
stays open for up to `maxRetries × maxRetryWaitMs` while the API keeps refusing. Await the call, or build that
client with `maxRetries: 0`.

**In a browser** a script may read `Retry-After` on a cross-origin response only when the server lists it in
`Access-Control-Expose-Headers`, and the API does not list it. A client running in a page therefore does not see
the header and does not retry: it throws on the first refusal, as it did before 0.1.4. Node, Deno, Bun and edge
runtimes see the header.

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
A transport may also return the response headers, `{ status, json, headers }`: a `Headers` object or a plain
record, where `Retry-After` is found under any case of its name, or another object with a `get` such as a `Map`,
which is asked for `retry-after` and for `Retry-After`. A transport that returns only `status` and `json` never
triggers a retry. The first attempt is given the body object itself and a retry a copy of what it
was when the call started. `sleep`, `random` and `now` replace the wait, the random addition and the clock, so a test of
your own retry handling does not have to sleep.

## Develop

```bash
cd sdk/js
npm install   # typescript and @types/node only
npm run build # compiles src/ to dist/
npm test      # compiles the node:test suite and runs it offline
```
