import { test } from "node:test";
import assert from "node:assert/strict";
import type { Inngest } from "inngest";

import { CommitLayerError } from "../src/index.js";
import { buildInngestAction, recordStepId, recordedStep, waitForVerdict, type StepLike } from "../src/inngest.js";
import { fakeApi, fakeLog, pendingVerification, settledVerification } from "./helpers.js";

const AT = "2026-10-15T10:42:00Z";

// The step tools an Inngest handler receives must satisfy StepLike. Compile-time only; never called.
export function _typeCheck(inngest: Inngest) {
  return inngest.createFunction({ id: "order-refund", triggers: [{ event: "order/refund.requested" }] }, async ({ step, runId }) => {
    const s: StepLike = step;
    void s;
    void runId;
  });
}

/** A fake `step`: runs every step at once, in order, and memoises results by id like a replay would. */
function fakeStep(memo = new Map<string, unknown>()) {
  const ran: string[] = [];
  const sleeps: [string, number | string][] = [];
  const step: StepLike = {
    async run(id, fn) {
      ran.push(id);
      if (memo.has(id)) return memo.get(id);
      const result: unknown = await fn();
      memo.set(id, JSON.parse(JSON.stringify(result ?? null)));
      return memo.get(id);
    },
    async sleep(id, time) {
      ran.push(id);
      if (!memo.has(id)) {
        sleeps.push([id, time]);
        memo.set(id, null);
      }
    },
  };
  return { step, ran, sleeps, memo };
}

// ---------------- body building ----------------

test("buildInngestAction builds exactly what POST /v1/actions accepts", () => {
  const body = buildInngestAction("refund", {
    runId: "01J9RUN",
    functionId: "order-refund",
    contract_id: "refund_workflow",
    subject_id: "order_10231",
    subject_kind: "order",
    action_type: "refund_order",
    agent_id: "order-agent",
    params: { amount: 180, currency: "USD" },
    now: () => AT,
  });
  assert.deepEqual(body, {
    action_id: "inngest:01J9RUN:refund",
    contract_id: "refund_workflow",
    workflow_id: "order-refund",
    agent_id: "order-agent",
    action_type: "refund_order",
    subject_id: "order_10231",
    subject_kind: "order",
    requested_at: AT,
    params: { amount: 180, currency: "USD", inngest_run_id: "01J9RUN", inngest_step_id: "refund" },
    channel: "inngest",
  });
});

test("buildInngestAction: explicit ids, compensates, contract_version, numeric subject", () => {
  const body = buildInngestAction("reissue", {
    runId: "r",
    contract_id: "c",
    subject_id: 42,
    action_id: "refund:42:retry",
    workflow_id: "wf_explicit",
    compensates: "inngest:r0:refund",
    contract_version: 2,
    now: () => AT,
  });
  assert.equal(body.action_id, "refund:42:retry");
  assert.equal(body.subject_id, "42");
  assert.equal(body.workflow_id, "wf_explicit");
  assert.equal(body.compensates, "inngest:r0:refund");
  assert.equal(body.contract_version, 2);
  assert.equal(body.agent_id, undefined);
  assert.deepEqual(body.params, { inngest_run_id: "r", inngest_step_id: "reissue" });
});

// ---------------- recordedStep ----------------

const options = (api: ReturnType<typeof fakeApi>, over: Record<string, unknown> = {}) => ({
  client: api.client,
  runId: "01J9RUN",
  functionId: "order-refund",
  contract_id: "refund_workflow",
  subject_id: "order_10231",
  subject_kind: "order",
  params: { amount: 180 },
  now: () => AT,
  ...over,
});

test("recordedStep records in its own step before the business step and returns the step's result", async () => {
  const api = fakeApi();
  const { step, ran } = fakeStep();
  const order: string[] = [];
  const result = await recordedStep(
    step,
    "refund",
    async () => {
      order.push("fn");
      assert.equal(api.posts().length, 1, "recorded before the step ran");
      return { refundId: "re_1" };
    },
    options(api, { onRecorded: () => order.push("recorded") }),
  );
  assert.deepEqual(result, { refundId: "re_1" });
  assert.deepEqual(order, ["recorded", "fn"]);
  assert.deepEqual(ran, [recordStepId("refund"), "refund"]);
  assert.equal(api.calls.length, 1, "nothing is sent after the step");
  assert.deepEqual(api.posts()[0]!.body, {
    action_id: "inngest:01J9RUN:refund",
    contract_id: "refund_workflow",
    workflow_id: "order-refund",
    subject_id: "order_10231",
    subject_kind: "order",
    requested_at: AT,
    params: { amount: 180, inngest_run_id: "01J9RUN", inngest_step_id: "refund" },
    channel: "inngest",
  });
});

test("recordedStep does not re-record on a memoised replay", async () => {
  const api = fakeApi();
  const memo = new Map<string, unknown>();
  let runs = 0;
  const fn = async () => ({ n: ++runs });
  const first = await recordedStep(fakeStep(memo).step, "refund", fn, options(api));
  const replay = await recordedStep(fakeStep(memo).step, "refund", fn, options(api));
  assert.deepEqual(first, { n: 1 });
  assert.deepEqual(replay, { n: 1 });
  assert.equal(runs, 1);
  assert.equal(api.posts().length, 1);
});

test("recordedStep is fail-open by default: the record step memoises the failure and the step runs", async () => {
  const api = fakeApi({ postStatus: 500 });
  const { lines, log } = fakeLog();
  const { step, memo } = fakeStep();
  const result = await recordedStep(step, "refund", () => "done", options(api, { log }));
  assert.equal(result, "done");
  assert.deepEqual(memo.get(recordStepId("refund")), {
    recorded: false,
    action_id: "inngest:01J9RUN:refund",
    error: "internal_error (500): store unavailable",
  });
  assert.equal(lines.length, 1);
  assert.match(lines[0]!, /recording failed for inngest:01J9RUN:refund \(refund\): .*: step runs anyway/);
});

test("recordedStep with failOpen: false throws from the record step and never runs the business step", async () => {
  const api = fakeApi({ postStatus: 500 });
  const { log } = fakeLog();
  const { step, ran } = fakeStep();
  let ranFn = false;
  await assert.rejects(
    recordedStep(step, "refund", () => (ranFn = true), options(api, { log, failOpen: false })),
    (e: unknown) => e instanceof CommitLayerError && e.code === "internal_error",
  );
  assert.equal(ranFn, false);
  assert.deepEqual(ran, [recordStepId("refund")]);
});

// ---------------- waitForVerdict ----------------

test("waitForVerdict polls in steps with sleeps between them until settled", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1"), pendingVerification("a1", 2), settledVerification("a1", "PASS")] });
  const { step, ran, sleeps } = fakeStep();
  const verdict = await waitForVerdict(step, "a1", { client: api.client, intervalMs: 10_000, timeoutMs: 120_000 });
  assert.equal(verdict.settled, true);
  assert.equal(verdict.timedOut, false);
  assert.equal(verdict.outcome, "PASS");
  assert.equal(verdict.policy, "PASS");
  assert.equal(verdict.actionId, "a1");
  assert.deepEqual(ran, [
    "commitlayer:a1:poll:0",
    "commitlayer:a1:sleep:0",
    "commitlayer:a1:poll:1",
    "commitlayer:a1:sleep:1",
    "commitlayer:a1:poll:2",
  ]);
  assert.deepEqual(sleeps, [
    ["commitlayer:a1:sleep:0", 10_000],
    ["commitlayer:a1:sleep:1", 10_000],
  ]);
  assert.equal(api.getsMade().length, 3);
  assert.equal(api.getsMade()[0]!.path, "/v1/actions/a1/wait?timeout=10");
});

test("waitForVerdict gives up after timeoutMs with the provisional verdict and timedOut", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1")] });
  const { step, sleeps } = fakeStep();
  const verdict = await waitForVerdict(step, "a1", { client: api.client, intervalMs: 5_000, timeoutMs: 10_000 });
  assert.equal(verdict.settled, false);
  assert.equal(verdict.timedOut, true);
  assert.equal(verdict.status, "pending");
  assert.equal(verdict.outcome, "UNKNOWN");
  assert.equal(sleeps.length, 2, "polls at 0s, 5s and 10s, then the timeout");
  assert.equal(api.getsMade().length, 3);
});

test("waitForVerdict replays from memoised steps without calling the API", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1"), settledVerification("a1")] });
  const memo = new Map<string, unknown>();
  const first = await waitForVerdict(fakeStep(memo).step, "a1", { client: api.client, intervalMs: 1_000 });
  const replay = await waitForVerdict(fakeStep(memo).step, "a1", { client: api.client, intervalMs: 1_000 });
  assert.equal(first.settled, true);
  assert.deepEqual(replay, first);
  assert.equal(api.getsMade().length, 2);
});

test("waitForVerdict uses the given step id prefix", async () => {
  const api = fakeApi({ gets: [settledVerification("a1")] });
  const { step, ran } = fakeStep();
  await waitForVerdict(step, "a1", { client: api.client, stepId: "verify-refund" });
  assert.deepEqual(ran, ["verify-refund:poll:0"]);
});
