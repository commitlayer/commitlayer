import { test } from "node:test";
import assert from "node:assert/strict";

import { Client, CommitLayerError, VERSION, type Json, type Transport } from "../src/index.js";

interface Call {
  method: string;
  path: string;
  body: Json | null | undefined;
}

/** A transport that records every call and answers with a fixed response, or throws. */
function fakeTransport(reply: { status: number; json: unknown } | Error = { status: 201, json: { ok: true } }) {
  const calls: Call[] = [];
  const transport: Transport = async (method, path, body) => {
    calls.push({ method, path, body });
    if (reply instanceof Error) throw reply;
    return reply;
  };
  return { calls, transport };
}

function client(reply?: { status: number; json: unknown } | Error, agentId?: string) {
  const t = fakeTransport(reply);
  return { ...t, cl: new Client({ transport: t.transport, agentId }) };
}

test("version", () => {
  assert.equal(VERSION, "0.1.0");
});

test("constructor requires a key unless a transport is injected", () => {
  const prev = process.env.COMMITLAYER_API_KEY;
  delete process.env.COMMITLAYER_API_KEY;
  try {
    assert.throws(() => new Client({}), /apiKey is required/);
  } finally {
    if (prev !== undefined) process.env.COMMITLAYER_API_KEY = prev;
  }
  const cl = new Client({ apiKey: "spk_test", apiUrl: "https://example.test/" });
  assert.equal(cl.apiUrl, "https://example.test");
  assert.equal(cl.timeoutMs, 20_000);
});

test("recordAction: body shape and defaults", async () => {
  const { cl, calls } = client(undefined, "support-bot");
  const res = await cl.recordAction("refund_workflow", "order_1", { params: { amount: 5 } });
  assert.deepEqual(res, { ok: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].path, "/v1/actions");
  const body = calls[0].body!;
  assert.deepEqual(Object.keys(body).sort(), [
    "action_id",
    "agent_id",
    "contract_id",
    "params",
    "requested_at",
    "subject_id",
  ]);
  assert.match(body.action_id as string, /^act_[0-9a-f]{32}$/);
  assert.match(body.requested_at as string, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.equal(body.contract_id, "refund_workflow");
  assert.equal(body.subject_id, "order_1");
  assert.deepEqual(body.params, { amount: 5 });
  assert.equal(body.agent_id, "support-bot"); // the client-level default
});

test("recordAction: every option maps to its snake_case key; subject id is stringified", async () => {
  const { cl, calls } = client();
  await cl.recordAction("c", 42 as unknown as string, {
    actionId: "refund:42",
    actionType: "refund_order",
    subjectKind: "order",
    workflowId: "wf_1",
    agentId: "agent-x",
    requestedAt: "2026-10-13T10:00:00Z",
    channel: "whatsapp",
    compensates: "refund:41",
  });
  assert.deepEqual(calls[0].body, {
    action_id: "refund:42",
    contract_id: "c",
    subject_id: "42",
    params: {},
    requested_at: "2026-10-13T10:00:00Z",
    action_type: "refund_order",
    subject_kind: "order",
    workflow_id: "wf_1",
    agent_id: "agent-x",
    channel: "whatsapp",
    compensates: "refund:41",
  });
});

test("recordAction: compensates is absent unless given", async () => {
  const { cl, calls } = client();
  await cl.recordAction("c", "s");
  assert.ok(!("compensates" in calls[0].body!));
  assert.ok(!("agent_id" in calls[0].body!));
});

test("pushFacts: facts, links and watermarks", async () => {
  const { cl, calls } = client({ status: 201, json: { ingested: 1 } });
  const facts = [{ source: "crm", subject_id: "order_1", fact_type: "note_added", occurred_at: "2026-10-13T10:42:09Z" }];
  await cl.pushFacts(facts, { watermarks: { crm: "2026-10-13T10:45:00Z" } });
  assert.equal(calls[0].path, "/v1/facts");
  assert.deepEqual(calls[0].body, { facts, watermarks: { crm: "2026-10-13T10:45:00Z" } });

  await cl.pushFacts(facts, { links: [{ subject_id: "order_1", linked_subject_id: "ticket_9" }] });
  assert.deepEqual(calls[1].body, { facts, links: [{ subject_id: "order_1", linked_subject_id: "ticket_9" }] });

  await cl.pushFacts(facts, { links: [], watermarks: {} });
  assert.deepEqual(calls[2].body, { facts });
});

test("verify: keys and dry_run default", async () => {
  const { cl, calls } = client({ status: 200, json: {} });
  await cl.verify({ actionId: "a1" });
  assert.equal(calls[0].path, "/v1/actions/verify");
  assert.deepEqual(calls[0].body, { dry_run: false, action_id: "a1" });

  const action = { contract_id: "c", subject_id: "s" };
  const facts = [{ source: "crm", subject_id: "s", fact_type: "t", occurred_at: "2026-10-13T10:00:00Z" }];
  await cl.verify({ action, facts, links: [{ subject_id: "s", linked_subject_id: "s2" }], dryRun: true });
  assert.deepEqual(calls[1].body, { dry_run: true, action, facts, links: [{ subject_id: "s", linked_subject_id: "s2" }] });
});

test("getAction: GET by ref", async () => {
  const { cl, calls } = client({ status: 200, json: { status: "settled" } });
  const v = await cl.getAction("refund:order_1");
  assert.deepEqual(v, { status: "settled" });
  assert.deepEqual(calls[0], { method: "GET", path: "/v1/actions/refund:order_1", body: undefined });
});

test("draftContract: key names and defaults", async () => {
  const { cl, calls } = client({ status: 200, json: { yaml: "" } });
  await cl.draftContract("refund_order");
  assert.equal(calls[0].path, "/v1/contracts/draft");
  assert.deepEqual(calls[0].body, {
    action_type: "refund_order",
    contract_id: null,
    lookback_days: 30,
    lookahead: "24h",
    min_share: 0.8,
  });
  await cl.draftContract(undefined, { contractId: "refund_workflow", lookbackDays: 7, lookahead: "1h", minShare: 0.5 });
  assert.deepEqual(calls[1].body, {
    action_type: null,
    contract_id: "refund_workflow",
    lookback_days: 7,
    lookahead: "1h",
    min_share: 0.5,
  });
});

test("validateContract and saveContract", async () => {
  const { cl, calls } = client({ status: 200, json: { valid: true } });
  await cl.validateContract("id: x");
  await cl.saveContract("id: x");
  assert.deepEqual(calls[0], { method: "POST", path: "/v1/contracts/validate", body: { yaml: "id: x" } });
  assert.deepEqual(calls[1], { method: "POST", path: "/v1/contracts", body: { yaml: "id: x" } });
});

test("errors map to CommitLayerError", async () => {
  const { cl } = client({
    status: 404,
    json: { error: { code: "unknown_contract", message: "no such contract", details: { id: "nope" } } },
  });
  await assert.rejects(cl.getAction("x"), (e: unknown) => {
    assert.ok(e instanceof CommitLayerError);
    assert.equal(e.name, "CommitLayerError");
    assert.equal(e.status, 404);
    assert.equal(e.code, "unknown_contract");
    assert.equal(e.message, "no such contract");
    assert.deepEqual(e.details, { id: "nope" });
    assert.equal(String(e), "unknown_contract (404): no such contract");
    return true;
  });

  const plain = client({ status: 502, json: "bad gateway" });
  await assert.rejects(plain.cl.getAction("x"), (e: unknown) => {
    assert.ok(e instanceof CommitLayerError);
    assert.equal(e.status, 502);
    assert.equal(e.code, "error");
    assert.equal(e.message, '"bad gateway"');
    return true;
  });
});

// ---------------- the action wrapper ----------------

const recorded = { action: { action_id: "a" }, verification: { summary: { headline: "PENDING" } } };

test("action wrapper: records before fn runs, result untouched, lastRecorded set", async () => {
  const order: string[] = [];
  const t = fakeTransport({ status: 201, json: recorded });
  const transport: Transport = async (...a) => {
    order.push("record");
    return t.transport(...a);
  };
  const cl = new Client({ transport });
  const seen: Json[] = [];

  const refund = cl.action("refund_workflow", {
    subject: (orderId: string) => orderId,
    params: (_orderId: string, amount: number) => ({ amount }),
    subjectKind: "order",
    workflowId: (orderId: string) => `wf_${orderId}`,
    onRecorded: (r) => seen.push(r),
  })(async (orderId: string, amount: number) => {
    order.push("fn");
    return { refunded: orderId, amount };
  });

  assert.equal(refund.lastRecorded, null);
  const out = await refund("order_1", 180);
  assert.deepEqual(out, { refunded: "order_1", amount: 180 });
  assert.deepEqual(order, ["record", "fn"]);
  assert.deepEqual(refund.lastRecorded, recorded);
  assert.deepEqual(seen, [recorded]);
  const body = t.calls[0].body!;
  assert.equal(body.contract_id, "refund_workflow");
  assert.equal(body.subject_id, "order_1");
  assert.deepEqual(body.params, { amount: 180 });
  assert.equal(body.subject_kind, "order");
  assert.equal(body.workflow_id, "wf_order_1");
  assert.match(body.action_id as string, /^act_/);
});

test("action wrapper: sync fn is awaited too, subject by argument index", async () => {
  const { cl, calls } = client({ status: 201, json: recorded });
  const f = cl.action("c", { subject: 1 })((a: number, id: string) => `${id}:${a}`);
  const p = f(7, "s9");
  assert.ok(p instanceof Promise);
  assert.equal(await p, "s9:7");
  assert.equal(calls[0].body!.subject_id, "s9");
});

test("action wrapper: idempotent actionId and compensates", async () => {
  const { cl, calls } = client({ status: 201, json: recorded });
  const f = cl.action("c", {
    subject: (id: string) => id,
    actionId: (id: string) => `refund:${id}`,
    compensates: (id: string) => `charge:${id}`,
  })(async (id: string) => id);
  await f("o1");
  await f("o1");
  assert.equal(calls.length, 2);
  for (const c of calls) {
    assert.equal(c.body!.action_id, "refund:o1");
    assert.equal(c.body!.compensates, "charge:o1");
  }

  const g = cl.action("c", { subject: 0, compensates: "act_prev" })(async (id: string) => id);
  await g("o2");
  assert.equal(calls[2].body!.compensates, "act_prev");
});

test("action wrapper: failOpen true warns and still runs fn; lastRecorded is null", async () => {
  const { cl } = client(new Error("network down"));
  const warnings: unknown[] = [];
  const origWarn = console.warn;
  console.warn = (...a: unknown[]) => void warnings.push(a.join(" "));
  try {
    let ran = false;
    const f = cl.action("c", { subject: 0 })(async function doWork(id: string) {
      ran = true;
      return id.toUpperCase();
    });
    assert.equal(await f("x"), "X");
    assert.ok(ran);
    assert.equal(f.lastRecorded, null);
    assert.equal(warnings.length, 1);
    assert.match(String(warnings[0]), /commitlayer: action not recorded for doWork: Error: network down/);
  } finally {
    console.warn = origWarn;
  }
});

test("action wrapper: failOpen false rethrows and fn does not run", async () => {
  const { cl } = client({ status: 402, json: { error: { code: "entitlement_limit", message: "used up" } } });
  let ran = false;
  const f = cl.action("c", { subject: 0, failOpen: false })(async (id: string) => {
    ran = true;
    return id;
  });
  await assert.rejects(f("x"), (e: unknown) => e instanceof CommitLayerError && e.code === "entitlement_limit");
  assert.equal(ran, false);
  assert.equal(f.lastRecorded, null);
});

test("action wrapper: a later failure resets lastRecorded", async () => {
  let fail = false;
  const transport: Transport = async () => {
    if (fail) throw new Error("down");
    return { status: 201, json: recorded };
  };
  const cl = new Client({ transport });
  const origWarn = console.warn;
  console.warn = () => {};
  try {
    const f = cl.action("c", { subject: 0 })(async (id: string) => id);
    await f("a");
    assert.deepEqual(f.lastRecorded, recorded);
    fail = true;
    await f("a");
    assert.equal(f.lastRecorded, null);
  } finally {
    console.warn = origWarn;
  }
});

test("waitForSettlement loops over the wait endpoint until it stops timing out", async () => {
  const calls: Call[] = [];
  let n = 0;
  const transport: Transport = async (method, path, body) => {
    calls.push({ method, path, body });
    n += 1;
    return { status: 200, json: n < 3 ? { status: "pending", timed_out: true } : { status: "settled", timed_out: false, lenses: { outcome: { verdict: "PASS" } } } };
  };
  const cl = new Client({ transport });
  const v = await cl.waitForSettlement("refund:o1", { timeoutMs: 600_000 });
  assert.equal(v.status, "settled");
  assert.equal(calls.length, 3);
  assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].path, "/v1/actions/refund:o1/wait?timeout=60");
});

test("waitForSettlement gives up at its own deadline and returns the pending view", async () => {
  const { cl, calls } = client({ status: 200, json: { status: "pending", timed_out: true } });
  const v = await cl.waitForSettlement("refund:o1", { timeoutMs: 0 });
  assert.equal(v.timed_out, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/v1/actions/refund:o1/wait?timeout=0");
});
