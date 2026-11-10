import { test } from "node:test";
import assert from "node:assert/strict";
import type { ActivityInterceptorsFactory } from "@temporalio/worker";

import { PathError } from "../src/index.js";
import {
  buildTemporalAction,
  commitlayerActivityInterceptors,
  createCommitLayerActivities,
  parseTemporalMapping,
  recordActivity,
  type ActivityContextLike,
  type ActivityInfoLike,
  type TemporalMapping,
} from "../src/temporal.js";
import { fakeApi, fakeLog, pendingVerification, settledVerification } from "./helpers.js";

const info = (over: Partial<ActivityInfoLike> = {}): ActivityInfoLike => ({
  activityId: "7",
  activityType: "refundOrder",
  attempt: 1,
  workflowExecution: { workflowId: "wf_cancel_10231", runId: "run_abc" },
  workflowType: "cancelAndRefund",
  taskQueue: "orders",
  ...over,
});

const mapping: TemporalMapping = {
  agent_id: "order-agent",
  activities: {
    refundOrder: {
      contract_id: "refund_workflow",
      subject: "args.0.orderId",
      subject_kind: "order",
      action_type: "refund_order",
      params: { amount: "args.0.amount", currency: "args.0.currency", reason: "args.1" },
    },
    reissueRefund: {
      contract_id: "refund_workflow",
      subject: "args.0.orderId",
      action_id: "refund:{args.0.orderId}:retry",
      compensates: "args.0.compensates",
      contract_version: 2,
    },
  },
};

const AT = "2026-10-15T10:42:00Z";

// The factory and its interceptors must be what the worker accepts. Compile-time only.
const _factory: ActivityInterceptorsFactory = commitlayerActivityInterceptors({ mapping, client: fakeApi().client });
void _factory;

// ---------------- body building ----------------

test("buildTemporalAction builds exactly what POST /v1/actions accepts", () => {
  const body = buildTemporalAction(
    mapping.activities.refundOrder!,
    { args: [{ orderId: "order_10231", amount: 180, currency: "USD" }, "customer_cancelled"], info: info() },
    { agentId: "order-agent", requestedAt: AT },
  );
  assert.deepEqual(body, {
    action_id: "temporal:wf_cancel_10231:7",
    contract_id: "refund_workflow",
    workflow_id: "wf_cancel_10231",
    agent_id: "order-agent",
    action_type: "refund_order",
    subject_id: "order_10231",
    subject_kind: "order",
    requested_at: AT,
    params: { amount: 180, currency: "USD", reason: "customer_cancelled", temporal_activity_id: "7", temporal_attempt: 1 },
    channel: "temporal",
  });
});

test("buildTemporalAction: templates, compensates, contract_version, workflow_id override, missing params left out", () => {
  const body = buildTemporalAction(
    mapping.activities.reissueRefund!,
    { args: [{ orderId: 42, compensates: "temporal:wf_cancel_10231:7" }], info: info({ activityType: "reissueRefund", activityId: "9", attempt: 3 }) },
    { workflowId: "wf_override", requestedAt: AT },
  );
  assert.equal(body.action_id, "refund:42:retry");
  assert.equal(body.subject_id, "42");
  assert.equal(body.compensates, "temporal:wf_cancel_10231:7");
  assert.equal(body.contract_version, 2);
  assert.equal(body.workflow_id, "wf_override");
  assert.equal(body.agent_id, undefined);
  assert.deepEqual(body.params, { temporal_activity_id: "9", temporal_attempt: 3 });
});

test("buildTemporalAction: a retry is the same action", () => {
  const call = (attempt: number) => ({ args: [{ orderId: "order_1", amount: 1 }], info: info({ attempt }) });
  const first = buildTemporalAction(mapping.activities.refundOrder!, call(1), { requestedAt: AT });
  const retry = buildTemporalAction(mapping.activities.refundOrder!, call(2), { requestedAt: AT });
  assert.equal(first.action_id, retry.action_id);
  assert.equal(retry.params.temporal_attempt, 2);
});

test("buildTemporalAction throws PathError for a missing subject or a standalone activity with the default id", () => {
  assert.throws(() => buildTemporalAction(mapping.activities.refundOrder!, { args: [{}], info: info() }), PathError);
  assert.throws(() => buildTemporalAction(mapping.activities.refundOrder!, { args: [], info: info() }), PathError);
  assert.throws(
    () => buildTemporalAction(mapping.activities.refundOrder!, { args: [{ orderId: "o" }], info: info({ workflowExecution: undefined }) }),
    /template path "workflowId" is missing/,
  );
  const explicit = buildTemporalAction(
    { contract_id: "c", subject: "args.0.orderId", action_id: "standalone:{activityId}" },
    { args: [{ orderId: "o" }], info: info({ workflowExecution: undefined }) },
    { requestedAt: AT },
  );
  assert.equal(explicit.action_id, "standalone:7");
  assert.equal(explicit.workflow_id, undefined);
});

test("parseTemporalMapping validates a JSON document", () => {
  const parsed = parseTemporalMapping({
    agent_id: "a",
    activities: { refundOrder: { contract_id: "c", subject: "args.0.id", params: { amount: "args.0.amount" }, contract_version: 1 } },
  });
  assert.equal(parsed.agent_id, "a");
  assert.equal(parsed.activities.refundOrder!.contract_version, 1);
  assert.throws(() => parseTemporalMapping({ activities: { x: { subject: "a" } } }), /contract_id is required/);
  assert.throws(() => parseTemporalMapping({ activities: { x: { contract_id: "c" } } }), /subject is required/);
  assert.throws(() => parseTemporalMapping({ activities: { x: { contract_id: "c", subject: "s", params: { a: 1 } } } }), /params.a must be a path/);
  assert.throws(() => parseTemporalMapping({ activities: { x: { contract_id: "c", subject: "s", contract_version: "1" } } }), /must be an integer/);
  assert.throws(() => parseTemporalMapping({}), /activities must be an object/);
});

// ---------------- the interceptor ----------------

interface Input {
  args: unknown[];
  headers: Record<string, unknown>;
}

function execution(over: Partial<ActivityInfoLike> = {}) {
  const order: string[] = [];
  const input: Input = { args: [{ orderId: "order_10231", amount: 180, currency: "USD" }, "customer_cancelled"], headers: {} };
  const next = async (i: Input) => {
    order.push("activity");
    assert.equal(i, input, "the input is passed through untouched");
    return { refundId: "re_1Q2x", status: "succeeded" };
  };
  const ctx: ActivityContextLike = { info: info(over), heartbeat: () => undefined };
  return { order, input, next, ctx };
}

test("interceptor records a mapped activity before it runs and never sends the result", async () => {
  const api = fakeApi();
  const { order, input, next, ctx } = execution();
  const recorded: string[] = [];
  const factory = commitlayerActivityInterceptors({
    mapping,
    client: api.client,
    now: () => AT,
    onRecorded: (_rec, body) => {
      order.push("recorded");
      recorded.push(body.action_id);
    },
  });
  const result = await factory(ctx).inbound.execute(input, next);
  assert.deepEqual(result, { refundId: "re_1Q2x", status: "succeeded" });
  assert.deepEqual(order, ["recorded", "activity"]);
  assert.deepEqual(recorded, ["temporal:wf_cancel_10231:7"]);
  assert.equal(api.calls.length, 1, "one POST, nothing after the activity");
  assert.deepEqual(api.posts()[0]!.body, {
    action_id: "temporal:wf_cancel_10231:7",
    contract_id: "refund_workflow",
    workflow_id: "wf_cancel_10231",
    agent_id: "order-agent",
    action_type: "refund_order",
    subject_id: "order_10231",
    subject_kind: "order",
    requested_at: AT,
    params: { amount: 180, currency: "USD", reason: "customer_cancelled", temporal_activity_id: "7", temporal_attempt: 1 },
    channel: "temporal",
  });
});

test("interceptor takes the info from the injected provider before the worker's context", async () => {
  const api = fakeApi();
  const { input, next, ctx } = execution();
  const factory = commitlayerActivityInterceptors({ mapping, client: api.client, info: () => info({ activityId: "99" }) });
  await factory(ctx).inbound.execute(input, next);
  assert.equal(api.posts()[0]!.body!.action_id, "temporal:wf_cancel_10231:99");
  // and without a context at all
  await factory().inbound.execute(input, next);
  assert.equal(api.posts().length, 2);
});

test("interceptor leaves an unmapped activity alone", async () => {
  const api = fakeApi();
  const { order, input, next, ctx } = execution({ activityType: "sendEmail" });
  const result = await commitlayerActivityInterceptors({ mapping, client: api.client })(ctx).inbound.execute(input, next);
  assert.deepEqual(order, ["activity"]);
  assert.equal(api.calls.length, 0);
  assert.ok(result);
});

test("interceptor runs the activity without recording when the subject is missing, and says so", async () => {
  const api = fakeApi();
  const { lines, log } = fakeLog();
  const { ctx } = execution();
  const order: string[] = [];
  const missing: Input = { args: [{}], headers: {} };
  const next = async (i: Input) => {
    assert.equal(i, missing);
    order.push("activity");
    return "ran";
  };
  const result = await commitlayerActivityInterceptors({ mapping, client: api.client, log })(ctx).inbound.execute(missing, next);
  assert.equal(result, "ran");
  assert.deepEqual(order, ["activity"]);
  assert.equal(api.calls.length, 0);
  assert.match(lines[0]!, /subject path "args.0.orderId" is missing/);
});

test("interceptor is fail-open by default: a recording failure is logged and the activity runs", async () => {
  const api = fakeApi({ postStatus: 500 });
  const { lines, log } = fakeLog();
  const { order, input, next, ctx } = execution();
  const result = await commitlayerActivityInterceptors({ mapping, client: api.client, log })(ctx).inbound.execute(input, next);
  assert.deepEqual(order, ["activity"]);
  assert.ok(result);
  assert.equal(lines.length, 1);
  assert.match(lines[0]!, /recording failed for temporal:wf_cancel_10231:7 \(refundOrder\): internal_error \(500\): store unavailable: activity runs anyway/);
});

test("interceptor with failOpen: false fails the activity non-retryably before it runs", async () => {
  const api = fakeApi({ postStatus: 500 });
  const { log } = fakeLog();
  const { order, input, next, ctx } = execution();
  const run = commitlayerActivityInterceptors({ mapping, client: api.client, log, failOpen: false })(ctx).inbound.execute(input, next);
  await assert.rejects(run, (e: Error & { nonRetryable?: boolean; type?: string }) => {
    assert.equal(e.name, "ApplicationFailure");
    assert.equal(e.nonRetryable, true);
    assert.equal(e.type, "CommitLayerRecordingFailed");
    assert.match(e.message, /CommitLayer recording failed for temporal:wf_cancel_10231:7/);
    assert.doesNotMatch(e.message, /spk_/);
    return true;
  });
  assert.deepEqual(order, [], "the activity did not run");
});

test("recordActivity returns the body on success and undefined when nothing was recorded", async () => {
  const api = fakeApi();
  const body = await recordActivity({ mapping, now: () => AT }, api.client, info(), [{ orderId: "o1" }]);
  assert.equal(body?.action_id, "temporal:wf_cancel_10231:7");
  assert.equal(await recordActivity({ mapping }, api.client, info({ activityType: "other" }), []), undefined);
});

// ---------------- the verifier activity ----------------

function fakeContext(onSleep: (ms: number) => void = () => undefined) {
  const heartbeats: unknown[] = [];
  const sleeps: number[] = [];
  const ctx: ActivityContextLike = {
    info: info({ activityType: "commitlayerVerify", activityId: "8" }),
    heartbeat: (d) => void heartbeats.push(d),
    sleep: async (ms) => {
      sleeps.push(ms);
      onSleep(ms);
    },
  };
  return { ctx, heartbeats, sleeps };
}

test("commitlayerVerify polls until settled, heartbeating after every poll and sleeping through the context", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1"), pendingVerification("a1", 3), settledVerification("a1", "PASS")] });
  const { ctx, heartbeats, sleeps } = fakeContext();
  const { commitlayerVerify } = createCommitLayerActivities({ client: api.client, context: () => ctx });
  const verdict = await commitlayerVerify("a1", { timeoutMs: 60_000, intervalMs: 2_000 });
  assert.equal(verdict.settled, true);
  assert.equal(verdict.timedOut, false);
  assert.equal(verdict.outcome, "PASS");
  assert.equal(verdict.policy, "PASS");
  assert.deepEqual(verdict.reasons.outcome, ["effect_observed:stripe.refund_created"]);
  assert.equal(api.getsMade().length, 3);
  // Each poll is held on the completion gate for the interval, so no local pause is needed between polls.
  assert.equal(api.getsMade()[2]!.path, "/v1/actions/a1/wait?timeout=2");
  assert.deepEqual(sleeps, []);
  assert.equal(heartbeats.length, 3);
  assert.deepEqual(heartbeats[2], { polls: 3, status: "settled", outcome: "PASS", policy: "PASS" });
});

test("commitlayerVerify returns the provisional verdict with timedOut once the timeout passes", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1")] });
  let clock = 0;
  const { ctx, sleeps } = fakeContext((ms) => (clock += ms));
  const { commitlayerVerify } = createCommitLayerActivities({
    client: api.client,
    context: () => ctx,
    now: () => clock,
    intervalMs: 5_000,
    holdMs: 0, // plain polling: the fake clock advances through the context's sleep
  });
  const verdict = await commitlayerVerify("a1", { timeoutMs: 10_000 });
  assert.equal(verdict.settled, false);
  assert.equal(verdict.timedOut, true);
  assert.equal(verdict.status, "pending");
  assert.equal(verdict.outcome, "UNKNOWN");
  assert.match(verdict.headline!, /^PENDING/);
  assert.deepEqual(sleeps, [5_000, 5_000]);
  assert.equal(api.getsMade().length, 3);
});

test("commitlayerVerify reports a FAIL on the policy lens with its reasons", async () => {
  const api = fakeApi({ gets: [settledVerification("a1", "PASS", "FAIL")] });
  const { ctx } = fakeContext();
  const { commitlayerVerify } = createCommitLayerActivities({ client: api.client, context: () => ctx });
  const verdict = await commitlayerVerify("a1");
  assert.equal(verdict.outcome, "PASS");
  assert.equal(verdict.policy, "FAIL");
  assert.deepEqual(verdict.reasons.policy, ["limit_exceeded:amount"]);
  assert.equal(api.getsMade().length, 1);
});

test("commitlayerVerify lets an API error propagate so the activity's retry policy applies", async () => {
  const api = fakeApi({ getStatus: 404 });
  const { ctx } = fakeContext();
  const { commitlayerVerify } = createCommitLayerActivities({ client: api.client, context: () => ctx });
  await assert.rejects(commitlayerVerify("missing"), /not_found \(404\)/);
});
