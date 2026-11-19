// Live contract check, TypeScript: each package's core calls against a running CommitLayer API.
//
// Skipped unless COMMITLAYER_LIVE=1, with COMMITLAYER_API_URL and COMMITLAYER_API_KEY naming a disposable workspace
// (scripts/boot_backend.py starts one and writes the three). It runs the packages as built (`npm run build` in
// sdk/js, recorder/js and integrations/js first), so it checks what would be published.
//
//   set -a; . build/live.env; set +a
//   node --test tests/live/
//
// Fast and deterministic: no sleeps, every wait bounded to a second or two, ids unique per run.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

const live = process.env.COMMITLAYER_LIVE === "1";
const skip = live ? false : "live API check: set COMMITLAYER_LIVE=1";
const CONTRACT = process.env.COMMITLAYER_LIVE_CONTRACT || "refund_workflow";
const RUN = randomUUID().replace(/-/g, "").slice(0, 10);
const VERDICTS = new Set(["PASS", "FAIL", "UNKNOWN"]);
const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

const sdk = live ? await import("../../sdk/js/dist/index.js") : null;
const integrations = live ? await import("../../integrations/js/dist/index.js") : null;
const recorder = live ? await import("../../recorder/js/dist/index.js") : null;
const client = live ? new sdk.Client({ maxRetries: 0, timeoutMs: 30_000 }) : null;

const actionId = `act_live_js_${RUN}`;
const subject = `order_live_js_${RUN}`;
let recorded;

test("recordAction answers action, verification and allowance", { skip }, async () => {
  recorded = await client.recordAction(CONTRACT, subject, {
    actionId,
    params: { amount: 42.5, currency: "USD" },
    actionType: "refund_order",
    subjectKind: "order",
    workflowId: `wf_live_js_${RUN}`,
    agentId: "live-smoke-agent",
    channel: "api",
  });
  for (const k of ["action", "verification", "allowance"]) assert.ok(k in recorded, k);
  assert.equal(recorded.action.action_id, actionId);
  assert.equal(recorded.action.subject_id, subject);
});

test("pushFacts accepts the fact", { skip }, async () => {
  const r = await client.pushFacts(
    [
      {
        source: "stripe",
        subject_id: subject,
        subject_kind: "order",
        fact_type: "refund",
        occurred_at: now(),
        attributes: { amount: 42.5, currency: "USD" },
        raw_ref: `live-js:${RUN}`,
      },
    ],
    { watermarks: { stripe: now() } },
  );
  assert.equal(r.accepted_count, 1);
  assert.equal(r.rejected_count, 0);
});

test("the atomic batch is checked first and stored whole", { skip }, async () => {
  const fact = {
    source: "stripe",
    subject_id: subject,
    subject_kind: "order",
    fact_type: "refund",
    occurred_at: now(),
    attributes: { amount: 42.5, currency: "USD" },
    raw_ref: `live-js-batches:${RUN}`,
  };
  const pre = await client.preflightBatches({ facts: [fact], observedThrough: { stripe: now() } });
  assert.equal(pre.ok, true);
  assert.equal(pre.written, false);
  const out = await client.publishBatches({ facts: [fact], observedThrough: { stripe: now() } });
  assert.equal(out.status, "committed");
  assert.equal(out.counts.facts, 1);
  await assert.rejects(
    client.publishBatches({ facts: [{ ...fact, raw_ref: `live-js-batches-bad:${RUN}`, occurred_at: "not-a-time" }] }),
    (e) => e.code === "batch_rejected" && e.details.written === false,
  );
});

test("getAction is a verification summarizeVerification understands", { skip }, async () => {
  const v = await client.getAction(actionId);
  assert.equal(v.action.action_id, actionId);
  for (const lens of ["outcome", "policy"]) {
    assert.ok(VERDICTS.has(v.lenses[lens].verdict), lens);
    assert.ok(Array.isArray(v.lenses[lens].reason_codes));
    assert.ok(Array.isArray(v.lenses[lens].reason_labels));
  }
  const s = integrations.summarizeVerification(v);
  assert.equal(s.actionId, actionId);
  assert.ok(["pending", "settled"].includes(s.status));
  assert.ok(VERDICTS.has(s.outcome) && VERDICTS.has(s.policy));
  assert.equal(typeof s.headline, "string");
});

test("waitForSettlement returns within its budget", { skip }, async () => {
  const v = await client.waitForSettlement(actionId, { timeoutMs: 1000 });
  assert.equal(typeof v.timed_out, "boolean");
  assert.equal(v.action.action_id, actionId);
});

test("verify: inline dry run and by id", { skip }, async () => {
  const dry = await client.verify({
    dryRun: true,
    action: {
      action_id: `act_live_js_dry_${RUN}`,
      contract_id: CONTRACT,
      subject_id: `order_js_dry_${RUN}`,
      subject_kind: "order",
      requested_at: now(),
      params: { amount: 1 },
    },
  });
  assert.equal(dry.dry_run, true);
  assert.ok(VERDICTS.has(dry.lenses.outcome.verdict));
  const byId = await client.verify({ actionId });
  assert.equal(byId.action.action_id, actionId);
});

test("contracts: draft and validate", { skip }, async () => {
  const d = await client.draftContract("refund_order", { lookbackDays: 1 });
  for (const k of ["yaml", "contract", "errors", "warnings", "stats"]) assert.ok(k in d, k);
  const v = await client.validateContract(d.yaml);
  // An invalid draft (no history to draft from) answers {valid: false, errors}; a valid one adds warnings.
  assert.equal(typeof v.valid, "boolean");
  assert.ok(Array.isArray(v.errors));
  if (v.valid) assert.ok(Array.isArray(v.warnings));
});

test("packs: list, get, configure, dryRun, coverage, recorderMapping; install refused without confirm", { skip }, async () => {
  const { items } = await client.packs.list();
  assert.ok(items.length > 0, "the pack library is seeded at start");
  const pack = items[0].id;
  assert.equal((await client.packs.get(pack)).id, pack);
  assert.equal((await client.packs.configure(pack)).pack_id, pack);
  const dry = await client.packs.dryRun(pack);
  assert.ok("total" in dry.summary && "ok" in dry.summary);
  assert.equal((await client.packs.coverage(pack)).pack_id, pack);
  assert.ok("tools" in (await client.packs.recorderMapping(pack)));
  await assert.rejects(client.packs.install(pack, {}, { confirm: false }), (e) => {
    assert.ok(e instanceof sdk.CommitLayerError);
    assert.ok(e.status >= 400 && e.status < 500 && e.code !== "error" && e.message);
    return true;
  });
});

test("an unknown action is a 404 with the error envelope", { skip }, async () => {
  await assert.rejects(client.getAction(`act_missing_${RUN}`), (e) => {
    assert.ok(e instanceof sdk.CommitLayerError);
    assert.equal(e.status, 404);
    assert.notEqual(e.code, "error");
    return true;
  });
});

test("recorder: a mapped tools/call is recorded before it is forwarded", { skip }, async () => {
  const lines = [];
  const rec = new recorder.Recorder({
    apiKey: process.env.COMMITLAYER_API_KEY,
    apiUrl: process.env.COMMITLAYER_API_URL,
    failOpen: false,
    log: (m) => lines.push(m),
    mapping: {
      agent_id: "live-recorder",
      tools: {
        refund_payment: {
          contract_id: CONTRACT,
          subject: "arguments.order_id",
          subject_kind: "order",
          action_type: "refund_order",
          params: { amount: "arguments.amount" },
          action_id: "mcp-live-js:{arguments.order_id}",
        },
      },
    },
  });
  const order = `order_rec_js_${RUN}`;
  const out = await rec.intercept(
    JSON.stringify({ jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "refund_payment", arguments: { order_id: order, amount: 5 } } }),
  );
  assert.deepEqual(out, { forward: true }, lines.join("\n"));
  const v = await client.getAction(`mcp-live-js:${order}`);
  assert.equal(v.action.subject_id, order);
  assert.equal(v.action.channel, "mcp");
});

test("integrations: record through sendAction, then poll on the gate", { skip }, async () => {
  const c = integrations.resolveClient({ apiKey: process.env.COMMITLAYER_API_KEY, apiUrl: process.env.COMMITLAYER_API_URL }, integrations.NO_RETRY);
  const id = `act_live_int_js_${RUN}`;
  const res = await integrations.sendAction(c, {
    action_id: id,
    contract_id: CONTRACT,
    subject_id: `order_int_js_${RUN}`,
    subject_kind: "order",
    action_type: "refund_order",
    requested_at: now(),
    params: { amount: 2 },
    channel: "temporal",
    workflow_id: `wf_int_js_${RUN}`,
  });
  assert.equal(res.action.action_id, id);
  const v = await integrations.pollUntilSettled(c, id, { timeoutMs: 1500, holdMs: 1000 });
  assert.equal(v.actionId, id);
  assert.ok(VERDICTS.has(v.outcome));
});
