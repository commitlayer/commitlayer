# @commitlayer/integrations

CommitLayer for workflow engines. Two subpaths, one package, zero runtime dependencies beyond the engine you
already run (`@temporalio/*` and `inngest` are optional peer dependencies; the matching SDK is loaded lazily, so
the package installs and works with either one present). Node 20+, ESM, TypeScript types included.

| Subpath | What it adds |
|---|---|
| `@commitlayer/integrations/temporal` | An activity interceptor that records a mapped activity as a CommitLayer action **before** it runs, and a `commitlayerVerify` activity that waits for the verdict. |
| `@commitlayer/integrations/inngest` | `recordedStep(step, "name", fn, {...})` and `waitForVerdict(step, actionRef, {...})` for Inngest functions. |
| `@commitlayer/integrations/x402` | `x402Observer({ recorder, evidence?, kind, contractVersion, decimals })` and `observer.wrap(fetch)`: records a purchase made over x402 from the protocol's headers and pushes its settlement and delivery facts. See [x402](#x402). |

Temporal makes the workflow durable. CommitLayer independently verifies the result.

The agent may declare its intent. It may read the verdict. It must never control the independent facts, the
governing contract, or the final evidence.

What that means in code: the integrations send the **intent** (`POST /v1/actions`: contract, subject, parameters,
request time) and read the **verdict** (`GET /v1/actions/{ref}`). They never send an activity's or a step's
result, never push facts, and never choose the contract version that judges them. CommitLayer verifies from the
systems of record (payments, orders, CRM), not from what the workflow says it did. See
[action contracts](https://commitlayer.ai/docs/action-contracts) for the contract model and
[the API reference](https://commitlayer.ai/docs/api#actions) for the request shapes.

## Install

```bash
npm install @commitlayer/integrations
export COMMITLAYER_API_KEY=spk_...     # a workspace API key; never written to logs
export COMMITLAYER_API_URL=https://api.commitlayer.ai   # the default
```

The package depends on the `commitlayer` SDK (`sdk/js`) and takes either a ready `Client` (`client: cl`) or the
`apiKey` / `apiUrl` options; with neither, the environment variables apply.

## What gets recorded

Every recorded action is exactly the `ActionIn` body of `POST /v1/actions`:

| Field | Temporal | Inngest |
|---|---|---|
| `action_id` | `temporal:{workflowId}:{activityId}` (template per activity) | `inngest:{runId}:{stepId}` (or `action_id`) |
| `contract_id`, `action_type`, `subject_kind` | from the activity's rule | from the options |
| `subject_id` | a path into the arguments (`args.0.orderId`) | `subject_id` |
| `params` | name → path, plus `temporal_activity_id`, `temporal_attempt` | `params`, plus `inngest_run_id`, `inngest_step_id` |
| `workflow_id` | the Temporal workflow id (or the mapping's `workflow_id`) | `functionId` (or `workflow_id`) |
| `agent_id` | the mapping's `agent_id` | `agent_id` |
| `requested_at` | now, UTC to the second, before the activity runs | now, before the step runs |
| `channel` | `temporal` | `inngest` |
| `compensates` | a path, sent when present | `compensates`, sent when given |

Retries share the `action_id`, and the API is idempotent on it: an activity that runs three times is one action
whose `requested_at` and `params` are those of the latest record. `contract_version` is accepted in both rule
shapes and pins the version the action is verified under; without it the API uses the contract's active version.

Recording is **fail-open** by default: when the API cannot be reached, a warning goes to `console.warn`
(`commitlayer: recording failed for <action_id> ...`; the message carries ids and error codes, never the key) and
the activity or step runs. The gap then shows up as an effect with no recorded action rather than a workflow that
stopped because its verifier was down. `failOpen: false` refuses the work before it starts (details per engine
below).

## Temporal

### 1. Record activities with the interceptor

Map activity types (the names you register on the worker) to contracts. Paths are dotted paths into
`{ args, info, workflowId, runId, activityId, activityType, attempt }`.

```ts
// worker.ts
import { Worker } from "@temporalio/worker";
import { commitlayerActivityInterceptors, createCommitLayerActivities } from "@commitlayer/integrations/temporal";
import * as activities from "./activities";

const mapping = {
  agent_id: "order-agent",
  activities: {
    refundOrder: {
      contract_id: "refund_workflow",
      action_type: "refund_order",
      subject: "args.0.orderId",
      subject_kind: "order",
      params: { amount: "args.0.amount", currency: "args.0.currency", reason: "args.1" },
      action_id: "refund:{args.0.orderId}",          // optional; default temporal:{workflowId}:{activityId}
    },
    reverseRefund: {
      contract_id: "refund_reversal",
      subject: "args.0.orderId",
      compensates: "args.0.compensates",             // the action id this one corrects
    },
  },
};

const worker = await Worker.create({
  taskQueue: "orders",
  workflowsPath: require.resolve("./workflows"),
  activities: { ...activities, ...createCommitLayerActivities() },   // adds commitlayerVerify
  interceptors: { activity: [commitlayerActivityInterceptors({ mapping })] },
});
await worker.run();
```

For each execution of a mapped activity the interceptor builds the body, records it, and only then calls the
activity. Unmapped activities are untouched. An execution whose subject path is missing is run without recording
and logged. The activity's return value is passed back unchanged and never sent anywhere.

`failOpen: false` throws `ApplicationFailure.nonRetryable(..., "CommitLayerRecordingFailed")` before the activity
runs, so the workflow sees the activity fail once and decides. The factory accepts `client`, `apiKey`, `apiUrl`,
`log`, `now`, `onRecorded(recorded, body)` and, for tests, `info: () => Info`; `parseTemporalMapping(json)`
validates a mapping loaded from a file. For the deprecated `interceptors.activityInbound` list use
`(ctx) => commitlayerActivityInterceptors(opts)(ctx).inbound`.

Standalone activities (no workflow) have no `workflowId`, so the default template cannot be rendered: give such a
rule its own `action_id`, e.g. `standalone:{activityId}`.

### 2. Gate completion on the verdict

`commitlayerVerify(actionRef, { timeoutMs, intervalMs })` is an activity: it waits on the API's completion gate
(`GET /v1/actions/{ref}/wait`, each call held for up to `intervalMs` and answered the moment the verdict settles;
`holdMs: 0` falls back to plain reads) with
`Context.current().sleep()` between polls and `Context.current().heartbeat()` after each, until the verification
`status` is `settled` or `timeoutMs` passes (default 15 minutes, 5 seconds between polls), then returns the verdict:

```ts
{ actionId, status: "pending" | "settled", settled, timedOut,
  outcome: "PASS" | "FAIL" | "UNKNOWN", policy: "PASS" | "FAIL" | "UNKNOWN",
  reasons: { outcome: string[], policy: string[] }, reasonLabels, headline, settlesAt,
  verification /* the full ActionVerification */ }
```

The workflow must not tell the customer "done" until this returns PASS. FAIL routes to a compensation activity.
An unsettled verdict past the timeout (`timedOut: true`, usually UNKNOWN) is the workflow's decision: escalate to a
person, wait longer, or complete with a flag, but not silently.

```ts
// workflows.ts  (the workflow sandbox: import only from @temporalio/workflow)
import { proxyActivities, workflowInfo } from "@temporalio/workflow";
import type { CommitLayerActivities } from "@commitlayer/integrations/temporal";
import type * as activities from "./activities";

const { refundOrder, reverseRefund, notifyOps } = proxyActivities<typeof activities>({
  startToCloseTimeout: "1 minute",
});
const { commitlayerVerify } = proxyActivities<CommitLayerActivities>({
  startToCloseTimeout: "20 minutes", // above timeoutMs
  heartbeatTimeout: "1 minute",      // above intervalMs
});

export async function cancelAndRefund(orderId: string, amount: number): Promise<string> {
  await refundOrder({ orderId, amount, currency: "USD" }, "customer_cancelled");

  // The action id the interceptor recorded: the rule's template, rendered from the same arguments.
  const verdict = await commitlayerVerify(`refund:${orderId}`, { timeoutMs: 15 * 60_000 });

  if (verdict.settled && verdict.policy === "PASS") return "refunded";
  if (verdict.policy === "FAIL") {
    await reverseRefund({ orderId, compensates: verdict.actionId });
    return "reversed";
  }
  await notifyOps({ orderId, workflowId: workflowInfo().workflowId, headline: verdict.headline, timedOut: verdict.timedOut });
  return "needs_review";
}
```

With the default template (`temporal:{workflowId}:{activityId}`) the workflow knows the id when it sets
`activityId` in the proxy options (`proxyActivities({ activityId: "refund", ... })`) and reads
`workflowInfo().workflowId`; otherwise a business-keyed template such as `refund:{args.0.orderId}` is the simplest.
`createCommitLayerActivities({ client, timeoutMs, intervalMs })` binds the activity to a client; the bare
`commitlayerVerify` export reads the environment on first call. An API error propagates and the activity's retry
policy applies; a timeout does not throw.

The verdict is PASS on `outcome` when every expected effect was observed, and on `policy` when additionally no
precondition, limit or prohibition fired. `reasons.policy` names what fired (`limit_exceeded:amount`,
`expected_effect_missing:shopify.order_cancelled`). The compensation itself is a recorded action
(`compensates: <action_id>`), so it gets its own verdict and the failed action is marked COMPENSATED only once that
verdict is PASS.

## Inngest

### 1. Record a step

```ts
import { Inngest } from "inngest";
import { recordedStep, waitForVerdict } from "@commitlayer/integrations/inngest";

const inngest = new Inngest({ id: "orders" });

export const refund = inngest.createFunction(
  { id: "order-refund", triggers: [{ event: "order/refund.requested" }] },   // v3: ({ id }, { event }, handler)
  async ({ event, step, runId }) => {
    const { orderId, amount } = event.data;

    const refund = await recordedStep(step, "refund", () => payments.refund(orderId, amount), {
      runId,
      functionId: "order-refund",            // the default workflow_id
      contract_id: "refund_workflow",
      action_type: "refund_order",
      subject_id: orderId,
      subject_kind: "order",
      params: { amount, currency: "USD", reason: event.data.reason },
      agent_id: "order-agent",
    });
    // ...
  },
);
```

`recordedStep` runs two steps: `refund:commitlayer`, which records the action (`inngest:<runId>:refund`), and
`refund`, which runs your function. Both are memoised by Inngest, so a replay of the function re-records nothing
and a retry of `refund` keeps the original `requested_at`, which stays before the first attempt's effects. The
record step memoises `{ recorded, action_id, error? }`; with `failOpen: false` it throws instead, Inngest retries
the record step, and the business step never runs until the record succeeds. `onRecorded(recorded, body)` fires
on the first (real) record only. `buildInngestAction(stepId, options)` is the pure body builder.

### 2. Wait for the verdict

```ts
    const verdict = await waitForVerdict(step, `inngest:${runId}:refund`, { timeoutMs: 15 * 60_000 });

    if (verdict.settled && verdict.policy === "PASS") return { status: "refunded", refund };
    if (verdict.policy === "FAIL") {
      await recordedStep(step, "reverse-refund", () => payments.reverse(refund.id), {
        runId, functionId: "order-refund",
        contract_id: "refund_reversal", subject_id: orderId, compensates: verdict.actionId ?? undefined,
      });
      return { status: "reversed", reasons: verdict.reasons.policy };
    }
    await step.run("notify-ops", () => ops.notify({ orderId, headline: verdict.headline, timedOut: verdict.timedOut }));
    return { status: "needs_review" };
```

`waitForVerdict` polls in its own steps: `commitlayer:<ref>:poll:0`, `commitlayer:<ref>:sleep:0`, `...:poll:1`
and so on, with `step.sleep(intervalMs)` between polls (default 5 seconds, `timeoutMs` default 15 minutes), so the
function is suspended rather than busy while it waits, and a replay walks the memoised polls without calling the
API. The number of polls is derived from the timeout and the interval, not from the clock, so it is identical on
every replay. Pass `stepId` to change the prefix when one run waits on several actions. The returned verdict has
the shape shown under Temporal above; `timedOut: true` means the wait gave up and the verdicts are provisional.
Same completion gate: no "done" to the customer before PASS, FAIL compensates, a timeout is the function's decision.

## x402

Agents that pay for a service over [x402](https://github.com/coinbase/x402) exchange three headers: the quote
(`PAYMENT-REQUIRED` on a 402), the payment (`PAYMENT-SIGNATURE` on the retry) and the settlement
(`PAYMENT-RESPONSE` on the final response). The observer wraps the `fetch` the x402 client library pays with,
records the purchase (`purchase_<kind>` on the subject `<nonce>`, with the quote's amount, asset, network, `payTo`,
resource and provider) and pushes a `settlement` fact (settled, pending for the facilitator's `settlement_pending`,
or failed) and a `delivered` or `delivery_failed` fact. It never pays, signs, retries, blocks, or keeps a body, a
signature or a key.

```ts
import { Client } from "commitlayer";
import { x402Observer } from "@commitlayer/integrations/x402";

const recorder = new Client({ apiKey: process.env.COMMITLAYER_RECORDER_KEY });   // actions:record
const observer = x402Observer({ recorder, kind: "api_call", contractVersion: 3, decimals: 6 });
const paidFetch = observer.wrap(x402Fetch);    // the x402 client's fetch, which pays and retries
const res = await paidFetch("https://api.example-data.com/v1/quote");
console.log(observer.last?.actionId, observer.last?.settlement, observer.last?.delivered);
```

Inside the agent's own process the facts travel with the recorder key and the service classes them
`agent_self_report`: shown, never counted as proof. In a gateway or tool server that terminates the 402 flow for
the agent, pass `evidence: new Client({ apiKey: EVIDENCE_KEY })` (a `facts:write` key) and the same facts are
`customer_backend`, which the x402 Service Delivery pack's contracts accept. `kind` is one of `api_call`,
`browser_session`, `dataset`, `model_inference`, `compute_resource`, `mcp_tool_call`.

## Testing your wiring

Nothing here needs a Temporal server or the Inngest dev server:

- `buildTemporalAction(rule, { args, info }, { agentId, workflowId, requestedAt })` and
  `buildInngestAction(stepId, options)` are pure. Assert on the body.
- `commitlayerActivityInterceptors({ mapping, client, info: () => fakeInfo })` runs against a fake `next`; the
  factory also takes the activity `Context` the worker hands it, so a `{ info, heartbeat }` object is enough.
- `createCommitLayerActivities({ client, context: () => fakeContext, now })` polls through the fake context's
  `sleep` and `heartbeat`.
- `recordedStep` and `waitForVerdict` take any object with `run(id, fn)` and `sleep(id, ms)`.
- A `Client` from `commitlayer` built with `{ transport }` answers every request from your test.

The package's own suite (`npm test`, `node --test`) does exactly this: 34 tests, settled-on-the-third-poll and
timeout paths included.

## Limitations

- **Intent and verdict only.** The integrations never push facts; connect the systems of record to CommitLayer
  (connectors or `POST /v1/facts` from the systems themselves). An activity that pushes its own "I did it" fact
  would only show the agent is self-consistent.
- **Verification is after the fact and read-only.** Nothing here blocks an activity or a step, and the verdict
  never reverses anything. Compensation is a workflow decision, made on the verdict, and is itself verified.
- **The verdict payload** includes the API's full verification (checks, evidence, up to 200 facts). It is stored
  in the workflow history or the step state; keep that in mind for very busy subjects.
- **Local activities** have no heartbeat; run `commitlayerVerify` as a regular activity.
