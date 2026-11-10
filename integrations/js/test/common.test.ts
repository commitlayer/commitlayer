import { test } from "node:test";
import assert from "node:assert/strict";

import {
  PathError,
  pollUntilSettled,
  renderTemplate,
  resolveClient,
  resolvePath,
  scalar,
  sendAction,
  summarizeVerification,
  type ActionBody,
} from "../src/index.js";
import { fakeApi, pendingVerification, settledVerification } from "./helpers.js";

test("resolvePath walks objects and arrays and returns undefined for a missing segment", () => {
  const root = { args: [{ orderId: "order_1", amount: 180 }], info: { attempt: 2 } };
  assert.equal(resolvePath(root, "args.0.orderId"), "order_1");
  assert.equal(resolvePath(root, "args.0.amount"), 180);
  assert.equal(resolvePath(root, "info.attempt"), 2);
  assert.equal(resolvePath(root, "args.1.orderId"), undefined);
  assert.equal(resolvePath(root, "args.0.orderId.x"), undefined);
  assert.equal(resolvePath(null, "a"), undefined);
});

test("renderTemplate fills placeholders and rejects missing or non-scalar values", () => {
  const root = { workflowId: "wf_1", activityId: "7", obj: { a: 1 } };
  assert.equal(renderTemplate("temporal:{workflowId}:{activityId}", root), "temporal:wf_1:7");
  assert.equal(renderTemplate("plain", root), "plain");
  assert.throws(() => renderTemplate("x:{missing}", root), PathError);
  assert.throws(() => renderTemplate("x:{obj}", root), PathError);
  assert.throws(() => scalar([1], "subject"), /must be a string, number or boolean, got array/);
  assert.equal(scalar(true, "x"), "true");
});

test("summarizeVerification reads lenses, status, reasons and headline; tolerates an empty body", () => {
  const v = summarizeVerification(settledVerification("act_1", "PASS", "FAIL"));
  assert.equal(v.actionId, "act_1");
  assert.equal(v.status, "settled");
  assert.equal(v.settled, true);
  assert.equal(v.timedOut, false);
  assert.equal(v.outcome, "PASS");
  assert.equal(v.policy, "FAIL");
  assert.deepEqual(v.reasons.policy, ["limit_exceeded:amount"]);
  assert.deepEqual(v.reasonLabels.outcome, ["Expected effect observed: stripe refund_created"]);
  assert.equal(v.headline, "FAIL — headline");
  assert.equal(v.settlesAt, "2026-10-15T10:57:00Z");

  const empty = summarizeVerification({});
  assert.equal(empty.status, "pending");
  assert.equal(empty.settled, false);
  assert.equal(empty.outcome, null);
  assert.deepEqual(empty.reasons, { outcome: [], policy: [] });
  assert.equal(empty.headline, null);
});

test("sendAction maps the body onto recordAction and omits absent optional fields", async () => {
  const api = fakeApi();
  const body: ActionBody = {
    action_id: "a1",
    contract_id: "refund_workflow",
    subject_id: "order_1",
    requested_at: "2026-10-15T10:42:00Z",
    params: { amount: 180 },
    channel: "temporal",
  };
  await sendAction(api.client, body);
  assert.deepEqual(api.posts()[0]!.body, {
    action_id: "a1",
    contract_id: "refund_workflow",
    subject_id: "order_1",
    requested_at: "2026-10-15T10:42:00Z",
    params: { amount: 180 },
    channel: "temporal",
  });

  await sendAction(api.client, {
    ...body,
    contract_version: 3,
    workflow_id: "wf",
    agent_id: "bot",
    action_type: "refund_order",
    subject_kind: "order",
    compensates: "a0",
  });
  const sent = api.posts()[1]!.body!;
  assert.equal(sent.workflow_id, "wf");
  assert.equal(sent.agent_id, "bot");
  assert.equal(sent.action_type, "refund_order");
  assert.equal(sent.subject_kind, "order");
  assert.equal(sent.compensates, "a0");
  assert.equal(sent.contract_version, 3);
});

test("resolveClient returns the given client and otherwise needs a key", () => {
  const api = fakeApi();
  assert.equal(resolveClient({ client: api.client }), api.client);
  const saved = process.env.COMMITLAYER_API_KEY;
  delete process.env.COMMITLAYER_API_KEY;
  try {
    assert.throws(() => resolveClient({}), /apiKey is required/);
  } finally {
    if (saved !== undefined) process.env.COMMITLAYER_API_KEY = saved;
  }
  const c = resolveClient({ apiKey: "spk_test", apiUrl: "https://example.test/" });
  assert.equal(c instanceof Object, true);
});

test("pollUntilSettled returns on the first settled poll and sleeps between polls", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1"), pendingVerification("a1", 2), settledVerification("a1")] });
  const sleeps: number[] = [];
  const polls: string[] = [];
  const v = await pollUntilSettled(api.client, "a1", {
    intervalMs: 50,
    timeoutMs: 10_000,
    sleep: async (ms) => void sleeps.push(ms),
    onPoll: (verdict) => void polls.push(verdict.status),
  });
  assert.equal(v.settled, true);
  assert.equal(v.outcome, "PASS");
  assert.equal(api.getsMade().length, 3);
  assert.deepEqual(sleeps, [50, 50]);
  assert.deepEqual(polls, ["pending", "pending", "settled"]);
  assert.equal(api.getsMade()[0]!.path, "/v1/actions/a1");
});

test("pollUntilSettled gives up after timeoutMs with the provisional verdict and timedOut", async () => {
  const api = fakeApi({ gets: [pendingVerification("a1")] });
  let clock = 0;
  const sleeps: number[] = [];
  const v = await pollUntilSettled(api.client, "a1", {
    intervalMs: 5_000,
    timeoutMs: 12_000,
    now: () => clock,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock += ms;
    },
  });
  assert.equal(v.settled, false);
  assert.equal(v.timedOut, true);
  assert.equal(v.status, "pending");
  assert.equal(v.outcome, "UNKNOWN");
  assert.deepEqual(sleeps, [5_000, 5_000, 2_000], "the last sleep is clipped to the remaining time");
  assert.equal(api.getsMade().length, 4);
});

test("pollUntilSettled propagates API errors", async () => {
  const api = fakeApi({ getStatus: 404 });
  await assert.rejects(pollUntilSettled(api.client, "nope", { timeoutMs: 0 }), /not_found/);
});
