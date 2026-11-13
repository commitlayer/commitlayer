import { test } from "node:test";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import assert from "node:assert/strict";

import {
  Client,
  CommitLayerError,
  VERSION,
  type ClientOptions,
  type Json,
  type ResponseHeaders,
  type RetryInfo,
  type Transport,
} from "../src/index.js";

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
  assert.equal(VERSION, JSON.parse(readFileSync(new URL("../../package.json", import.meta.url), "utf8")).version);
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

test("packs: the helpers call the pack routes and never activate", async () => {
  const calls: Array<[string, string, Json | null | undefined]> = [];
  const transport: Transport = async (method, path, body) => {
    calls.push([method, path, body]);
    return { status: 200, json: { ok: true } };
  };
  const c = new Client({ transport });
  await c.packs.list();
  await c.packs.get("x402_service_delivery");
  await c.packs.configure("x402_service_delivery", { purchase_api_call: { approved_limit: 2 } });
  await c.packs.dryRun("x402_service_delivery", {}, { workflow: "purchase_api_call" });
  await c.packs.install("x402_service_delivery", {}, { confirm: true });
  await c.packs.install("x402_service_delivery");
  await c.packs.coverage("x402_service_delivery");
  await c.packs.recorderMapping("x402_service_delivery");
  assert.deepEqual(
    calls.map(([m, p]) => [m, p]),
    [
      ["GET", "/v1/packs"],
      ["GET", "/v1/packs/x402_service_delivery"],
      ["POST", "/v1/packs/x402_service_delivery/configure"],
      ["POST", "/v1/packs/x402_service_delivery/dry-run"],
      ["POST", "/v1/packs/x402_service_delivery/install"],
      ["POST", "/v1/packs/x402_service_delivery/install"],
      ["GET", "/v1/packs/x402_service_delivery/coverage"],
      ["GET", "/v1/packs/x402_service_delivery/recorder-mapping"],
    ],
  );
  assert.deepEqual(calls[2][2], { configuration: { purchase_api_call: { approved_limit: 2 } } });
  assert.deepEqual(calls[3][2], { configuration: {}, workflow: "purchase_api_call" });
  assert.deepEqual(calls[4][2], { configuration: {}, confirm: true });
  assert.deepEqual(calls[5][2], { configuration: {}, confirm: false });
});

// ---------------- retry on 429 and 503 with Retry-After ----------------

type Answer = { status: number; json: unknown; headers?: ResponseHeaders | null };
type Reply = Answer | Error;

/** The API's two "not applied, come back" answers, as docs/API.md words them. */
function workspaceBusy(retryAfter?: string): Answer {
  const json = {
    error: {
      code: "workspace_busy",
      message: "This workspace already has 3 writes in progress; the request was not applied. Retry in a moment, or send fewer writes at once.",
      details: { in_progress: 3 },
    },
  };
  return { status: 429, json, headers: retryAfter === undefined ? undefined : { "Retry-After": retryAfter } };
}

function busy(retryAfter?: string): Answer {
  const json = { error: { code: "busy", message: "The service is busy; the request was not applied. Retry in a moment." } };
  return { status: 503, json, headers: retryAfter === undefined ? undefined : new Headers({ "Retry-After": retryAfter }) };
}

const created: Answer = { status: 201, json: recorded };

/**
 * A client over a transport that answers with each reply in turn (the last one repeats). Every call is kept with
 * the body as it serialised at that moment, and every wait is recorded instead of slept. The random addition is
 * zero unless the test sets it.
 */
function retrying(replies: Reply[], options: ClientOptions = {}) {
  const calls: (Call & { sent: string })[] = [];
  const sleeps: number[] = [];
  const transport: Transport = async (method, path, body) => {
    calls.push({ method, path, body, sent: JSON.stringify(body) });
    const reply = replies[Math.min(calls.length - 1, replies.length - 1)];
    if (reply instanceof Error) throw reply;
    return reply;
  };
  const cl = new Client({ transport, sleep: async (ms) => void sleeps.push(ms), random: () => 0, ...options });
  return { cl, calls, sleeps };
}

async function thrownBy(p: Promise<unknown>): Promise<CommitLayerError> {
  try {
    await p;
  } catch (e) {
    assert.ok(e instanceof CommitLayerError, `expected a CommitLayerError, got ${String(e)}`);
    return e;
  }
  return assert.fail("expected the call to throw");
}

test("retry: the defaults are 3 retries and a 10 second ceiling", () => {
  const cl = new Client({ apiKey: "spk_test" });
  assert.equal(cl.maxRetries, 3);
  assert.equal(cl.maxRetryWaitMs, 10_000);
  // A ceiling no timer can run for is taken as the longest one can: a longer delay would fire at once.
  assert.equal(new Client({ apiKey: "spk_test", maxRetryWaitMs: 3e9 }).maxRetryWaitMs, 2_147_483_647);
  assert.equal(new Client({ apiKey: "spk_test", maxRetryWaitMs: 2_147_483_647 }).maxRetryWaitMs, 2_147_483_647);
  const tuned = new Client({ apiKey: "spk_test", maxRetries: 1, maxRetryWaitMs: 2_500 });
  assert.equal(tuned.maxRetries, 1);
  assert.equal(tuned.maxRetryWaitMs, 2_500);
  assert.equal(new Client({ apiKey: "spk_test", maxRetries: 2.9 }).maxRetries, 2);
  for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => new Client({ apiKey: "spk_test", maxRetries: bad }), /maxRetries must be zero or more/);
    assert.throws(() => new Client({ apiKey: "spk_test", maxRetryWaitMs: bad }), /maxRetryWaitMs must be zero or more/);
  }
});

test("retry: 429 workspace_busy then success waits the stated second and sends the generated action_id again", async () => {
  const { cl, calls, sleeps } = retrying([workspaceBusy("1"), created]);
  const res = await cl.recordAction("refund_workflow", "order_1", { params: { amount: 5 } });
  assert.deepEqual(res, recorded);
  assert.equal(calls.length, 2);
  assert.deepEqual(sleeps, [1000]);
  assert.match(calls[0].body!.action_id as string, /^act_[0-9a-f]{32}$/); // generated by the client, once
  assert.equal(calls[1].sent, calls[0].sent); // the same body, so the same action_id and requested_at
  assert.deepEqual(calls[1].body, calls[0].body);
  assert.notEqual(calls[1].body, calls[0].body); // a copy taken when the call started, not the live object
  assert.deepEqual([calls[1].method, calls[1].path], ["POST", "/v1/actions"]);
});

test("retry: 503 busy then success, the header read from a Headers object, the caller's action_id kept", async () => {
  const { cl, calls, sleeps } = retrying([busy("1"), created]);
  const res = await cl.recordAction("refund_workflow", "order_1", { actionId: "refund:order_1" });
  assert.deepEqual(res, recorded);
  assert.equal(calls.length, 2);
  assert.deepEqual(sleeps, [1000]);
  assert.equal(calls[0].body!.action_id, "refund:order_1");
  assert.equal(calls[1].sent, calls[0].sent);
});

test("retry: the action wrapper generates its action_id once, whatever the number of attempts", async () => {
  const { cl, calls, sleeps } = retrying([workspaceBusy("1"), busy("1"), created]);
  let ran = 0;
  const refund = cl.action("refund_workflow", { subject: 0, failOpen: false })(async (orderId: string) => {
    ran += 1;
    return orderId;
  });
  assert.equal(await refund("order_1"), "order_1");
  assert.equal(ran, 1);
  assert.deepEqual(refund.lastRecorded, recorded);
  assert.equal(calls.length, 3);
  assert.deepEqual(sleeps, [1000, 1000]);
  assert.match(calls[0].body!.action_id as string, /^act_[0-9a-f]{32}$/);
  assert.deepEqual(new Set(calls.map((c) => c.sent)).size, 1);
});

test("retry: every method goes through it with an unchanged body", async () => {
  const facts = [{ source: "crm", subject_id: "order_1", fact_type: "note_added", occurred_at: "2026-10-13T10:42:09Z" }];
  const push = retrying([workspaceBusy("1"), { status: 201, json: { ingested: 1 } }]);
  assert.deepEqual(await push.cl.pushFacts(facts, { watermarks: { crm: "2026-10-13T10:45:00Z" } }), { ingested: 1 });
  assert.equal(push.calls.length, 2);
  assert.equal(push.calls[1].sent, push.calls[0].sent);

  const read = retrying([
    { status: 429, json: { error: { code: "rate_limited", message: "retry in 2 s" } }, headers: { "retry-after": "2" } },
    { status: 200, json: { status: "settled" } },
  ]);
  assert.deepEqual(await read.cl.getAction("refund:order_1"), { status: "settled" });
  assert.deepEqual(read.sleeps, [2000]);
  assert.deepEqual(read.calls.map((c) => [c.method, c.path]), [
    ["GET", "/v1/actions/refund:order_1"],
    ["GET", "/v1/actions/refund:order_1"],
  ]);
});

test("retry: a recording that names no action_id is sent once, because the API would give each attempt a new one", async () => {
  const action = { contract_id: "refund_workflow", subject_id: "order_1", requested_at: "2026-10-13T10:00:00Z" };

  // A body built by hand for the raw request: no action_id, no retry.
  const raw = retrying([busy("1"), created]);
  const e = await thrownBy(raw.cl.request("POST", "/v1/actions", action));
  assert.equal(raw.calls.length, 1);
  assert.deepEqual(raw.sleeps, []);
  assert.deepEqual([e.status, e.code, e.retryAfterMs, e.attempts], [503, "busy", 1000, 1]);

  // Whatever the spelling of the method and of the path: the route is read as the server reads it.
  const spellings: [string, string][] = [
    ["post", "/v1/actions"],
    ["Post", "/v1/actions"],
    ["POST", "/v1/actions?source=x"],
    ["POST", "/v1/actions/"],
    ["POST", "/v1/actions/?source=x"],
    ["POST", "/v1/actions#f"],
    ["POST", "/v1/actions?a=1#f"],
    ["POST", "/v1/./actions"],
    ["POST", "/v1/x/../actions"],
    ["POST", "/v1//actions"],
    ["POST", "/v1/%61ctions"],
    ["POST", "/v1/actions%2F"],
  ];
  for (const [method, path] of spellings) {
    const spelled = retrying([busy("1"), created]);
    await thrownBy(spelled.cl.request(method, path, action));
    assert.equal(spelled.calls.length, 1, `${method} ${path}`);
    assert.deepEqual(spelled.sleeps, []);
  }
  // A stray percent sign in the query does not hide the route; one in the path makes it another path, which
  // the API does not have, and nothing here throws over it.
  const stray = retrying([busy("1"), created]);
  await thrownBy(stray.cl.request("POST", "/v1/actions?note=100%", action));
  assert.equal(stray.calls.length, 1);
  const elsewhere = retrying([busy("1"), { status: 404, json: { error: { code: "not_found", message: "no" } } }]);
  assert.equal((await thrownBy(elsewhere.cl.request("POST", "/v1/actions%", action))).status, 404);
  assert.equal(elsewhere.calls.length, 2);

  // The same body with an action_id is repeatable, whatever the spelling of the method and the path.
  const named = retrying([busy("1"), created]);
  assert.deepEqual(await named.cl.request("post", "/v1/actions/?source=x", { ...action, action_id: "refund:order_1" }), recorded);
  assert.equal(named.calls.length, 2);
  // Another route, and another method on the same route, carry no recording: repeatable as they are.
  for (const [method, path] of [["POST", "/v1/actions/batch"], ["POST", "/v1/facts"], ["PUT", "/v1/actions"]]) {
    const other = retrying([busy("1"), created]);
    await other.cl.request(method, path, action);
    assert.equal(other.calls.length, 2, `${method} ${path}`);
  }

  // An inline verify that stores its action: retried only when the action names its id.
  const inline = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
  await thrownBy(inline.cl.verify({ action }));
  assert.equal(inline.calls.length, 1);
  const inlineNamed = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
  await inlineNamed.cl.verify({ action: { ...action, action_id: "refund:order_1" } });
  assert.equal(inlineNamed.calls.length, 2);

  // A dry run stores nothing, and a verify by id names the action: both are repeatable.
  const dry = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
  await dry.cl.verify({ action, dryRun: true });
  assert.equal(dry.calls.length, 2);
  const byId = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
  await byId.cl.verify({ actionId: "refund:order_1" });
  assert.equal(byId.calls.length, 2);

  // Only dry_run: true is a dry run. The API reads "false" and 0 as false and stores the action, so a body
  // written by hand with one of those is sent once; so is any other value that is not the boolean.
  for (const dryRun of ["false", 0, "", null, "true", 1]) {
    const odd = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
    await thrownBy(odd.cl.request("POST", "/v1/actions/verify", { dry_run: dryRun, action }));
    assert.equal(odd.calls.length, 1, `dry_run ${JSON.stringify(dryRun)}`);
  }
  const reallyDry = retrying([workspaceBusy("1"), { status: 200, json: {} }]);
  await reallyDry.cl.request("POST", "/v1/actions/verify/", { dry_run: true, action });
  assert.equal(reallyDry.calls.length, 2);
});

test("retry: a retry sends the request as it was made, whatever the caller does to its own objects during the wait", async () => {
  // The caller keeps the params object and changes it while the client waits.
  const params: Json = { amount: 180, lines: [{ sku: "a", qty: 1 }] };
  const changed = retrying([workspaceBusy("1"), busy("1"), created], {
    sleep: async () => {
      params.amount = 9999;
      (params.lines as Json[] | undefined)?.push({ sku: "b", qty: 2 });
      delete params.lines;
    },
  });
  await changed.cl.recordAction("refund_workflow", "order_1", { actionId: "refund:order_1", params });
  assert.equal(changed.calls.length, 3);
  assert.deepEqual(new Set(changed.calls.map((c) => c.sent)).size, 1);
  assert.deepEqual((JSON.parse(changed.calls[2].sent) as Json).params, { amount: 180, lines: [{ sku: "a", qty: 1 }] });
  assert.notEqual(changed.calls[2].body, changed.calls[1].body); // each retry gets a copy of its own

  // A buffer of facts emptied as soon as the push was started.
  const buffer: Json[] = [{ source: "crm", subject_id: "order_1", fact_type: "note_added", occurred_at: "2026-10-13T10:42:09Z" }];
  const push = retrying([workspaceBusy("1"), { status: 201, json: { ingested: 1 } }]);
  const pushing = push.cl.pushFacts(buffer);
  buffer.length = 0;
  await pushing;
  assert.equal(push.calls.length, 2);
  assert.equal(push.calls[1].sent, push.calls[0].sent);
  assert.equal(((JSON.parse(push.calls[1].sent) as Json).facts as Json[]).length, 1);

  // An inline action that loses its action_id during the wait is still the request that named one, and a
  // transport that changes the body it was given does not change the next attempt.
  const inline: Json = { action_id: "refund:order_1", contract_id: "refund_workflow", subject_id: "order_1" };
  const seen: string[] = [];
  const cl = new Client({
    transport: async (_method, _path, body) => {
      seen.push(JSON.stringify(body));
      delete (body!.action as Json).action_id;
      return seen.length < 3 ? workspaceBusy("1") : { status: 200, json: {} };
    },
    sleep: async () => {},
    random: () => 0,
  });
  await cl.verify({ action: inline });
  assert.equal(seen.length, 3);
  assert.equal(new Set(seen).size, 1);
  assert.ok(seen[0].includes("refund:order_1"));

  // The first attempt is given the caller's own object, as a transport written against 0.1.3 expects; only a
  // retry is given the copy.
  const own: Json = { facts: [] };
  const raw = retrying([workspaceBusy("1"), created]);
  await raw.cl.request("POST", "/v1/facts", own);
  assert.equal(raw.calls[0].body, own);
  assert.notEqual(raw.calls[1].body, own);
  assert.deepEqual(raw.calls[1].body, own);

  // Key order, numeric keys and characters JSON escapes survive the copy: the retry serialises to the same text.
  const awkward = retrying([workspaceBusy("1"), created]);
  await awkward.cl.recordAction("c", "s", { actionId: "a1", params: { b: 1, 2: "two", 1: "one", "": null, note: "\u2028 \ud800 \"q\"", n: 1.0, neg: -0, deep: { z: [1, [2, { y: null }]] } } });
  assert.equal(awkward.calls[1].sent, awkward.calls[0].sent);
});

test("retry: a body that has no JSON form is sent once", async () => {
  // Only a transport of the caller's own can send one; the client cannot fix what it would send again.
  const circular: Json = { contract_id: "c" };
  circular.self = circular;
  for (const body of [circular, { amount: 10n }] as Json[]) {
    const bodies: unknown[] = [];
    const cl = new Client({
      transport: async (_method, _path, given) => {
        bodies.push(given);
        return bodies.length === 1 ? workspaceBusy("1") : created;
      },
      sleep: async () => assert.fail("must not wait"),
    });
    const e = await thrownBy(cl.request("POST", "/v1/facts", body));
    assert.equal(bodies.length, 1);
    assert.deepEqual([e.status, e.retryAfterMs, e.attempts], [429, 1000, 1]);
    assert.equal(bodies[0], body); // and the first attempt is given the caller's own object, as before
  }
});

test("retry: the wait is the stated time plus at most twice as much, and never above the ceiling", async () => {
  const none = retrying([workspaceBusy("1"), created], { random: () => 0 });
  await none.cl.recordAction("c", "s");
  assert.deepEqual(none.sleeps, [1000]); // never less than stated

  const half = retrying([workspaceBusy("1"), created], { random: () => 0.5 });
  await half.cl.recordAction("c", "s");
  assert.deepEqual(half.sleeps, [2000]);

  const most = retrying([workspaceBusy("2"), created], { random: () => 0.999 });
  await most.cl.recordAction("c", "s");
  assert.deepEqual(most.sleeps, [5996]); // just under three times the stated two seconds

  // Close to the ceiling the addition is a share of what is left under it, so the waits stay spread out
  // below the ceiling and do not all end on it.
  const near = retrying([workspaceBusy("9"), created], { random: () => 0.5 });
  await near.cl.recordAction("c", "s");
  assert.deepEqual(near.sleeps, [9500]);
  const nearest = retrying([workspaceBusy("9"), created], { random: () => 0.999 });
  await nearest.cl.recordAction("c", "s");
  assert.deepEqual(nearest.sleeps, [9999]);
  const at = retrying([workspaceBusy("10"), created], { random: () => 0.999 });
  await at.cl.recordAction("c", "s");
  assert.deepEqual(at.sleeps, [10_000]);
  const own = retrying([workspaceBusy("1"), created], { random: () => 0.5, maxRetryWaitMs: 2_000 });
  await own.cl.recordAction("c", "s");
  assert.deepEqual(own.sleeps, [1500]); // one second stated, one second left under a ceiling of two

  // Every wait is drawn again: three refusals, three different additions.
  const draws = [0.1, 0.9, 0.5];
  const drawn = retrying([workspaceBusy("1"), workspaceBusy("1"), workspaceBusy("1"), created], { random: () => draws.shift()! });
  await drawn.cl.recordAction("c", "s");
  assert.deepEqual(drawn.sleeps, [1200, 2800, 2000]);

  // A random source that misbehaves adds nothing; the wait is never below what the server stated.
  for (const r of [-1, 7, Number.NaN]) {
    const odd = retrying([workspaceBusy("1"), created], { random: () => r });
    await odd.cl.recordAction("c", "s");
    assert.deepEqual(odd.sleeps, [1000]);
  }
});

test("retry: the default wait is a real timer that runs for the stated time, not less and not twice as long", async () => {
  let calls = 0;
  const cl = new Client({
    transport: async () => (++calls === 1 ? workspaceBusy("0.4") : created),
    random: () => 0, // the stated wait alone
  });
  const started = performance.now();
  assert.deepEqual(await cl.recordAction("c", "s"), recorded);
  const elapsed = performance.now() - started;
  assert.equal(calls, 2);
  assert.ok(elapsed >= 399, `the retry came after ${elapsed} ms, before the stated 400`);
  assert.ok(elapsed < 800, `the retry came after ${elapsed} ms`);
});

test("retry: refusals past the limit throw the last answer, with the stated wait and the attempts made", async () => {
  const { cl, calls, sleeps } = retrying([workspaceBusy("1"), workspaceBusy("1"), workspaceBusy("1"), busy("2")]);
  const e = await thrownBy(cl.recordAction("c", "s", { actionId: "a1" }));
  assert.equal(calls.length, 4); // the first attempt and three retries
  assert.deepEqual(sleeps, [1000, 1000, 1000]);
  assert.equal(e.name, "CommitLayerError");
  assert.equal(e.status, 503); // the last answer, not the first
  assert.equal(e.code, "busy");
  assert.equal(e.message, "The service is busy; the request was not applied. Retry in a moment.");
  assert.equal(String(e), "busy (503): The service is busy; the request was not applied. Retry in a moment.");
  assert.equal(e.details, undefined);
  assert.equal(e.retryAfterMs, 2000);
  assert.equal(e.attempts, 4);
  assert.deepEqual(new Set(calls.map((c) => c.sent)).size, 1);

  const one = retrying([workspaceBusy("1")], { maxRetries: 1 });
  const e1 = await thrownBy(one.cl.recordAction("c", "s"));
  assert.equal(one.calls.length, 2);
  assert.equal(e1.status, 429);
  assert.equal(e1.code, "workspace_busy");
  assert.deepEqual(e1.details, { in_progress: 3 });
  assert.equal(e1.retryAfterMs, 1000);
  assert.equal(e1.attempts, 2);
});

test("retry: a 4xx other than 429 is not retried, with or without Retry-After", async () => {
  for (const status of [400, 401, 402, 403, 404, 408, 409, 410, 413, 422, 425, 428, 431, 451]) {
    const { cl, calls, sleeps } = retrying([
      { status, json: { error: { code: "conflict", message: "no" } }, headers: { "Retry-After": "1" } },
      created,
    ]);
    const e = await thrownBy(cl.recordAction("c", "s"));
    assert.equal(e.status, status);
    assert.equal(calls.length, 1, `status ${status} must not be retried`);
    assert.deepEqual(sleeps, []);
    assert.equal(e.attempts, 1);
    assert.equal(e.retryAfterMs, 1000); // what the server stated is still reported
  }
});

test("retry: 500 and the other 5xx are not retried, with or without Retry-After", async () => {
  for (const status of [500, 501, 502, 504, 507]) {
    const { cl, calls, sleeps } = retrying([
      { status, json: { error: { code: "internal_error", message: "boom" } }, headers: { "Retry-After": "1" } },
      created,
    ]);
    const e = await thrownBy(cl.recordAction("c", "s"));
    assert.equal(e.status, status);
    assert.equal(calls.length, 1, `status ${status} must not be retried`);
    assert.deepEqual(sleeps, []);
    assert.equal(e.attempts, 1);
  }
});

test("retry: 429 and 503 without Retry-After are not retried", async () => {
  const withoutHeader: Answer[] = [
    workspaceBusy(),
    busy(),
    { ...workspaceBusy(), headers: null },
    { ...workspaceBusy(), headers: {} },
    { ...busy(), headers: new Headers({ "X-Request-Id": "r1" }) },
    { ...busy(), headers: { "retry-after": undefined } },
  ];
  for (const reply of withoutHeader) {
    const { cl, calls, sleeps } = retrying([reply, created]);
    const e = await thrownBy(cl.recordAction("c", "s"));
    assert.equal(calls.length, 1);
    assert.deepEqual(sleeps, []);
    assert.equal(e.retryAfterMs, undefined);
    assert.equal(e.attempts, 1);
  }
});

test("retry: a transport that returns only status and json behaves as before", async () => {
  // The shape every transport written against 0.1.3 returns: no headers, so never a retry.
  const calls: Call[] = [];
  const transport: Transport = async (method, path, body): Promise<{ status: number; json: unknown }> => {
    calls.push({ method, path, body });
    return calls.length === 1
      ? { status: 429, json: { error: { code: "workspace_busy", message: "busy", details: { in_progress: 3 } } } }
      : { status: 201, json: recorded };
  };
  const cl = new Client({ transport, sleep: async () => assert.fail("must not wait") });
  const e = await thrownBy(cl.recordAction("c", "s"));
  assert.equal(calls.length, 1);
  assert.deepEqual([e.status, e.code, e.message, e.details], [429, "workspace_busy", "busy", { in_progress: 3 }]);
  assert.equal(String(e), "workspace_busy (429): busy");
  assert.deepEqual(await cl.recordAction("c", "s"), recorded); // and a success is a success
});

test("retry: a Retry-After above the ceiling is neither waited for nor retried", async () => {
  const over = retrying([workspaceBusy("11"), created]);
  const e = await thrownBy(over.cl.recordAction("c", "s"));
  assert.equal(over.calls.length, 1);
  assert.deepEqual(over.sleeps, []);
  assert.equal(e.retryAfterMs, 11_000);
  assert.equal(e.attempts, 1);

  const at = retrying([workspaceBusy("10"), created]);
  await at.cl.recordAction("c", "s");
  assert.deepEqual(at.sleeps, [10_000]); // exactly the ceiling is still waited for

  // The free plan's daily cap states the seconds to midnight UTC: an error at once, not a call that blocks.
  const fairUse = retrying([
    { status: 429, json: { error: { code: "fair_use_exceeded", message: "tomorrow" } }, headers: { "Retry-After": "41234" } },
    created,
  ]);
  const cap = await thrownBy(fairUse.cl.pushFacts([]));
  assert.equal(fairUse.calls.length, 1);
  assert.deepEqual(fairUse.sleeps, []);
  assert.equal(cap.code, "fair_use_exceeded");
  assert.equal(cap.retryAfterMs, 41_234_000);

  const own = retrying([workspaceBusy("3"), created], { maxRetryWaitMs: 2_000 });
  const e2 = await thrownBy(own.cl.recordAction("c", "s"));
  assert.equal(own.calls.length, 1);
  assert.deepEqual(own.sleeps, []);
  assert.equal(e2.retryAfterMs, 3000);

  // A later answer above the ceiling ends the retries there, with the attempts made so far.
  const later = retrying([workspaceBusy("1"), workspaceBusy("60"), created]);
  const e3 = await thrownBy(later.cl.recordAction("c", "s"));
  assert.equal(later.calls.length, 2);
  assert.deepEqual(later.sleeps, [1000]);
  assert.equal(e3.retryAfterMs, 60_000);
  assert.equal(e3.attempts, 2);
});

test("retry: maxRetries 0 turns it off", async () => {
  const { cl, calls, sleeps } = retrying([workspaceBusy("1"), created], { maxRetries: 0 });
  const e = await thrownBy(cl.recordAction("c", "s"));
  assert.equal(calls.length, 1);
  assert.deepEqual(sleeps, []);
  assert.equal(e.code, "workspace_busy");
  assert.equal(e.retryAfterMs, 1000); // still reported, for a caller that retries on its own
  assert.equal(e.attempts, 1);
});

test("retry: zero and decimal seconds", async () => {
  const cases: [string, number][] = [
    ["0", 0],
    ["0.5", 500],
    ["1.5", 1500],
    ["1.1", 1100],
    [".25", 250],
    ["2.", 2000],
    [" 3 ", 3000],
    ["0.0004", 1], // rounded up: never less than stated
    ["0.29", 290],
    ["2.007", 2007], // 2.007 * 1000 is 2007.0000000000002 as a float: still 2007, not 2008
  ];
  for (const [header, ms] of cases) {
    const { cl, calls, sleeps } = retrying([workspaceBusy(header), created]);
    await cl.recordAction("c", "s");
    assert.equal(calls.length, 2, `Retry-After ${JSON.stringify(header)}`);
    assert.deepEqual(sleeps, [ms], `Retry-After ${JSON.stringify(header)}`);
  }
});

test("retry: an HTTP-date Retry-After is waited until, by the client's clock", async () => {
  const now = Date.UTC(2026, 9, 21, 7, 27, 57); // Wed, 21 Oct 2026 07:27:57 GMT
  const ahead = retrying([workspaceBusy("Wed, 21 Oct 2026 07:28:00 GMT"), created], { now: () => now });
  await ahead.cl.recordAction("c", "s");
  assert.deepEqual(ahead.sleeps, [3000]);

  const past = retrying([busy("Wed, 21 Oct 2026 07:27:00 GMT"), created], { now: () => now });
  await past.cl.recordAction("c", "s");
  assert.deepEqual(past.sleeps, [0]); // a time already passed: retry now

  // A clock that gives no number leaves no wait to read: the answer is thrown, nothing waits for "NaN" ms.
  const noClock = retrying([workspaceBusy("Wed, 21 Oct 2026 07:28:00 GMT"), created], { now: () => Number.NaN });
  const lost = await thrownBy(noClock.cl.recordAction("c", "s"));
  assert.equal(noClock.calls.length, 1);
  assert.deepEqual(noClock.sleeps, []);
  assert.equal(lost.retryAfterMs, undefined);

  const far = retrying([workspaceBusy("Wed, 21 Oct 2026 07:29:00 GMT"), created], { now: () => now });
  const e = await thrownBy(far.cl.recordAction("c", "s"));
  assert.equal(far.calls.length, 1); // 63 s away: above the ceiling
  assert.deepEqual(far.sleeps, []);
  assert.equal(e.retryAfterMs, 63_000);
});

test("retry: garbage, negative and enormous Retry-After values are never waited for", async () => {
  const unreadable = [
    "",
    " ",
    "soon",
    "-1",
    "-0.5",
    "+1",
    "1e3",
    "0x10",
    "1,5",
    "1, 1",
    "1 second",
    "NaN",
    "Infinity",
    "2026-10-21T07:28:00Z",
    "Wed, 21 Foo 2026 07:28:00 GMT",
    "Wed, 21 Oct 2026 07:28:00",
    "9".repeat(400), // beyond what a number holds
  ];
  for (const header of unreadable) {
    const { cl, calls, sleeps } = retrying([workspaceBusy(header), created]);
    const e = await thrownBy(cl.recordAction("c", "s"));
    assert.equal(calls.length, 1, `Retry-After ${JSON.stringify(header.slice(0, 40))}`);
    assert.deepEqual(sleeps, []);
    assert.equal(e.retryAfterMs, undefined, `Retry-After ${JSON.stringify(header.slice(0, 40))}`);
    assert.equal(e.attempts, 1);
  }
  for (const header of ["86400", "99999999999999999999", "Fri, 31 Dec 9999 23:59:59 GMT"]) {
    const { cl, calls, sleeps } = retrying([workspaceBusy(header), created]);
    const e = await thrownBy(cl.recordAction("c", "s"));
    assert.equal(calls.length, 1, `Retry-After ${header}`);
    assert.deepEqual(sleeps, []);
    assert.ok(e.retryAfterMs !== undefined && e.retryAfterMs > 10_000, `Retry-After ${header}`);
  }
});

test("retry: header names are matched without regard to case, in a record, a Headers or anything with get", async () => {
  const shapes: ResponseHeaders[] = [
    { "Retry-After": "1" },
    { "retry-after": "1" },
    { "RETRY-AFTER": "1" },
    { "Retry-After": ["1", "30"] }, // the first value, as node:http gives repeated headers
    { "Retry-After": 1 } as unknown as ResponseHeaders, // a number where the type asks for text is still read
    new Headers({ "RETRY-after": "1" }),
    { get: (name: string) => (name === "retry-after" ? "1" : null) },
    // A get that knows one spelling only, as a Map does: asked for both.
    new Map([["Retry-After", "1"]]),
    new Map([["retry-after", "1"]]),
    { get: (name: string) => (name === "Retry-After" ? "1" : undefined) },
  ];
  for (const headers of shapes) {
    const { cl, calls, sleeps } = retrying([{ ...workspaceBusy(), headers }, created]);
    await cl.recordAction("c", "s");
    assert.equal(calls.length, 2);
    assert.deepEqual(sleeps, [1000]);
  }

  // Headers that cannot be read are no headers: the caller gets the API's answer, not the reader's own error.
  const broken: ResponseHeaders = {
    get: () => {
      throw new TypeError("headers are closed");
    },
  };
  const { cl, calls } = retrying([{ ...workspaceBusy(), headers: broken }, created]);
  const e = await thrownBy(cl.recordAction("c", "s"));
  assert.equal(calls.length, 1);
  assert.deepEqual([e.status, e.code, e.retryAfterMs, e.attempts], [429, "workspace_busy", undefined, 1]);
});

test("retry: a network error or a timeout from the transport is not retried", async () => {
  for (const failure of [new TypeError("fetch failed"), new DOMException("This operation was aborted", "AbortError")]) {
    const { cl, calls, sleeps } = retrying([failure, created]);
    await assert.rejects(cl.recordAction("c", "s"), (e: unknown) => e === failure);
    assert.equal(calls.length, 1);
    assert.deepEqual(sleeps, []);
  }
  // Also after a refusal: the retry's own failure is what the caller gets.
  const failure = new TypeError("fetch failed");
  const { cl, calls } = retrying([workspaceBusy("1"), failure, created]);
  await assert.rejects(cl.recordAction("c", "s"), (e: unknown) => e === failure);
  assert.equal(calls.length, 2);
});

test("retry: onRetry is told once per retry and never sees the key or the body", async () => {
  const seen: RetryInfo[] = [];
  const { cl } = retrying([workspaceBusy("1"), busy("2"), created], {
    apiKey: "spk_secret_value",
    random: () => 0.5,
    onRetry: (info) => seen.push(info),
  });
  await cl.recordAction("refund_workflow", "order_1", { actionId: "refund:order_1", params: { amount: 180 } });
  assert.deepEqual(seen, [
    { method: "POST", path: "/v1/actions", status: 429, code: "workspace_busy", attempt: 1, maxRetries: 3, retryAfterMs: 1000, waitMs: 2000 },
    { method: "POST", path: "/v1/actions", status: 503, code: "busy", attempt: 2, maxRetries: 3, retryAfterMs: 2000, waitMs: 4000 },
  ]);
  assert.ok(!JSON.stringify(seen).includes("spk_secret_value"));
  assert.ok(!JSON.stringify(seen).includes("180"));

  const silent = retrying([workspaceBusy("1"), created]);
  await silent.cl.recordAction("c", "s"); // no callback: nothing is logged, nothing throws

  const throwing = retrying([workspaceBusy("1"), created], {
    onRetry: () => {
      throw new Error("logger down");
    },
  });
  assert.deepEqual(await throwing.cl.recordAction("c", "s"), recorded);
});

test("retry: action wrapper with failOpen logs the last refusal once the retries are used up and still runs fn", async () => {
  const { cl, calls } = retrying([workspaceBusy("1")], { maxRetries: 2 });
  const warnings: string[] = [];
  const origWarn = console.warn;
  console.warn = (...a: unknown[]) => void warnings.push(a.join(" "));
  try {
    const f = cl.action("c", { subject: 0 })(async function doWork(id: string) {
      return id.toUpperCase();
    });
    assert.equal(await f("x"), "X");
    assert.equal(calls.length, 3);
    assert.equal(f.lastRecorded, null);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /commitlayer: action not recorded for doWork: workspace_busy \(429\): /);
  } finally {
    console.warn = origWarn;
  }
});

// ---------------- the built-in fetch transport, against a real HTTP server ----------------

interface Seen {
  method: string;
  url: string;
  headers: IncomingMessage["headers"];
  raw: string;
}

/** A local HTTP server on an ephemeral port. `handle` answers each request; every request is kept with its raw body. */
async function withServer(
  handle: (seen: Seen, res: ServerResponse, all: Seen[]) => void,
  run: (url: string, all: Seen[]) => Promise<void>,
): Promise<void> {
  const all: Seen[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const seen = { method: req.method ?? "", url: req.url ?? "", headers: req.headers, raw: Buffer.concat(chunks).toString("utf8") };
      all.push(seen);
      handle(seen, res, all);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await run(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, all);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

function answer(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(body));
}

const BUSY_BODY = { error: { code: "busy", message: "The service is busy; the request was not applied. Retry in a moment." } };
const WORKSPACE_BUSY_BODY = { error: { code: "workspace_busy", message: "not applied", details: { in_progress: 3 } } };

test("fetch transport: Retry-After of a real 429 is read, waited and the same bytes are sent again", async () => {
  await withServer(
    (_seen, res, all) => {
      if (all.length === 1) answer(res, 429, WORKSPACE_BUSY_BODY, { "Retry-After": "1" });
      else answer(res, 201, recorded);
    },
    async (apiUrl, all) => {
      const sleeps: number[] = [];
      const cl = new Client({ apiKey: "spk_test", apiUrl, sleep: async (ms) => void sleeps.push(ms), random: () => 0 });
      const res = await cl.recordAction("refund_workflow", "order_1", { params: { amount: 180 } });
      assert.deepEqual(res, recorded);
      assert.equal(all.length, 2);
      assert.deepEqual(sleeps, [1000]);
      assert.equal(all[1].raw, all[0].raw); // byte for byte
      assert.match((JSON.parse(all[0].raw) as Json).action_id as string, /^act_[0-9a-f]{32}$/);
      for (const seen of all) {
        assert.equal(seen.method, "POST");
        assert.equal(seen.url, "/v1/actions");
        assert.equal(seen.headers["x-api-key"], "spk_test");
        assert.equal(seen.headers["user-agent"], `commitlayer-js/${VERSION}`);
      }
    },
  );
});

test("fetch transport: a real 503 past the limit surfaces the last error with the header's wait", async () => {
  await withServer(
    (_seen, res) => answer(res, 503, BUSY_BODY, { "Retry-After": "1" }),
    async (apiUrl, all) => {
      const sleeps: number[] = [];
      const cl = new Client({ apiKey: "spk_test", apiUrl, maxRetries: 2, sleep: async (ms) => void sleeps.push(ms), random: () => 0 });
      const e = await thrownBy(cl.recordAction("c", "s", { actionId: "a1" }));
      assert.equal(all.length, 3);
      assert.deepEqual(sleeps, [1000, 1000]);
      assert.deepEqual([e.status, e.code, e.retryAfterMs, e.attempts], [503, "busy", 1000, 3]);
      assert.equal(new Set(all.map((s) => s.raw)).size, 1);
    },
  );
});

test("fetch transport: no Retry-After, a 500 with one, and a non-JSON 503 with one", async () => {
  await withServer(
    (seen, res) => {
      if (seen.url === "/v1/actions/no-header") answer(res, 503, BUSY_BODY);
      else if (seen.url === "/v1/actions/five-hundred") answer(res, 500, { error: { code: "internal_error", message: "boom" } }, { "Retry-After": "1" });
      else {
        // What a proxy in front of the API may send: not JSON, and a wait above the ceiling.
        res.writeHead(503, { "content-type": "text/plain", "Retry-After": "120" });
        res.end("Service Unavailable");
      }
    },
    async (apiUrl, all) => {
      const sleeps: number[] = [];
      const cl = new Client({ apiKey: "spk_test", apiUrl, sleep: async (ms) => void sleeps.push(ms) });
      const a = await thrownBy(cl.getAction("no-header"));
      assert.deepEqual([a.status, a.code, a.retryAfterMs, a.attempts], [503, "busy", undefined, 1]);
      const b = await thrownBy(cl.getAction("five-hundred"));
      assert.deepEqual([b.status, b.code, b.retryAfterMs, b.attempts], [500, "internal_error", 1000, 1]);
      const c = await thrownBy(cl.getAction("proxy"));
      assert.deepEqual([c.status, c.code, c.message, c.retryAfterMs, c.attempts], [503, "http_error", "Service Unavailable", 120_000, 1]);
      assert.equal(all.length, 3);
      assert.deepEqual(sleeps, []);
    },
  );
});

test("fetch transport: an empty 429 with Retry-After is retried; a 201 that is not JSON is an error and is not sent again", async () => {
  await withServer(
    (seen, res, all) => {
      if (seen.url === "/v1/actions/empty" && all.length === 1) {
        // No body at all, as a proxy or a rate limiter in front of the API may answer.
        res.writeHead(429, { "Retry-After": "1" });
        res.end();
      } else if (seen.url === "/v1/actions/empty") answer(res, 200, { status: "settled" });
      else {
        res.writeHead(201, { "content-type": "text/html", "Retry-After": "1" });
        res.end("<html>created</html>");
      }
    },
    async (apiUrl, all) => {
      const sleeps: number[] = [];
      const cl = new Client({ apiKey: "spk_test", apiUrl, sleep: async (ms) => void sleeps.push(ms), random: () => 0 });
      assert.deepEqual(await cl.getAction("empty"), { status: "settled" });
      assert.equal(all.length, 2);
      assert.deepEqual(sleeps, [1000]);

      // The request was answered 201: whatever the body is, sending it again could apply it twice.
      const e = await thrownBy(cl.getAction("not-json"));
      assert.deepEqual([e.status, e.code, e.message, e.attempts], [201, "invalid_response", "<html>created</html>", undefined]);
      assert.equal(all.length, 3);
      assert.deepEqual(sleeps, [1000]);
    },
  );
});

test("fetch transport: the bytes of a retry are those of the first attempt, whatever the caller changed during the wait", async () => {
  await withServer(
    (_seen, res, all) => {
      if (all.length === 1) answer(res, 429, WORKSPACE_BUSY_BODY, { "Retry-After": "0.05" });
      else answer(res, 201, recorded);
    },
    async (apiUrl, all) => {
      const cl = new Client({ apiKey: "spk_test", apiUrl });
      const params: Json = { amount: 180 };
      const recording = cl.recordAction("refund_workflow", "order_1", { actionId: "refund:order_1", params });
      params.amount = 9999; // the caller reuses its object while the record is on its way
      assert.deepEqual(await recording, recorded);
      assert.equal(all.length, 2);
      assert.equal(all[1].raw, all[0].raw);
      assert.deepEqual((JSON.parse(all[1].raw) as Json).params, { amount: 180 });
    },
  );
});

test("fetch transport: each attempt has its own timeout and the wait is not counted against it", async () => {
  await withServer(
    (_seen, res, all) => {
      if (all.length === 1) answer(res, 429, WORKSPACE_BUSY_BODY, { "Retry-After": "0" });
      else if (all.length === 2) answer(res, 201, recorded);
      // the third and later requests are never answered
    },
    async (apiUrl, all) => {
      // A wait three times the per-attempt timeout: the second attempt still gets its whole timeout.
      const cl = new Client({ apiKey: "spk_test", apiUrl, timeoutMs: 100, sleep: () => new Promise((r) => setTimeout(r, 300)) });
      assert.deepEqual(await cl.recordAction("c", "s"), recorded);
      assert.equal(all.length, 2);

      // A timeout is not retried: one request, one AbortError.
      await assert.rejects(cl.recordAction("c", "s"), (e: unknown) => e instanceof Error && e.name === "AbortError");
      assert.equal(all.length, 3);
    },
  );
});

test("fetch transport: a burst past a gate of three is recorded once each; with retries off most of it is refused", async () => {
  const BURST = 30;
  const SLOTS = 3;
  let inProgress = 0;
  let hold = true; // while true an admitted request is held until the whole burst has arrived
  const waiting: (() => void)[] = [];
  const applied = new Map<string, number>();
  const sentBy = new Map<string, Set<string>>();
  await withServer(
    (seen, res, all) => {
      const id = (JSON.parse(seen.raw) as Json).action_id as string;
      sentBy.set(id, (sentBy.get(id) ?? new Set()).add(seen.raw));
      const finish = () => {
        inProgress -= 1;
        applied.set(id, (applied.get(id) ?? 0) + 1);
        answer(res, 201, recorded);
      };
      if (inProgress >= SLOTS) answer(res, 429, WORKSPACE_BUSY_BODY, { "Retry-After": "0.05" });
      else {
        inProgress += 1;
        if (hold) waiting.push(finish);
        else setTimeout(finish, 2);
      }
      if (hold && all.length >= BURST) {
        hold = false;
        for (const f of waiting.splice(0)) f();
      }
    },
    async (apiUrl, all) => {
      // Retries off: the three that found a slot succeed, the other twenty-seven are refused.
      const off = new Client({ apiKey: "spk_test", apiUrl, maxRetries: 0 });
      const first = await Promise.allSettled(Array.from({ length: BURST }, (_, i) => off.recordAction("c", `order_${i}`)));
      assert.equal(first.filter((r) => r.status === "fulfilled").length, SLOTS);
      const refused = first.filter((r): r is PromiseRejectedResult => r.status === "rejected").map((r) => r.reason as CommitLayerError);
      assert.equal(refused.length, BURST - SLOTS);
      assert.ok(refused.every((e) => e instanceof CommitLayerError && e.code === "workspace_busy" && e.retryAfterMs === 50));

      // Retries on, the real timer and the real random source: every action of the burst is recorded, once.
      all.length = 0;
      applied.clear();
      sentBy.clear();
      hold = true;
      const on = new Client({ apiKey: "spk_test", apiUrl, maxRetries: 12 });
      const second = await Promise.allSettled(Array.from({ length: BURST }, (_, i) => on.recordAction("c", `order_${i}`)));
      assert.deepEqual(second.filter((r) => r.status === "rejected"), []);
      assert.equal(applied.size, BURST);
      assert.ok([...applied.values()].every((n) => n === 1));
      assert.ok(all.length > BURST, "part of the burst must have been refused and sent again");
      assert.ok([...sentBy.values()].every((bodies) => bodies.size === 1), "every attempt of an action carries the same bytes");
    },
  );
});

test("fetch transport: after a retried call settles nothing keeps a Node process alive", async () => {
  await withServer(
    (_seen, res, all) => {
      if (all.length === 1) answer(res, 503, BUSY_BODY, { "Retry-After": "0.05" });
      else answer(res, 201, recorded);
    },
    async (apiUrl, all) => {
      // A separate process with the default timer and a long per-attempt timeout: it must print the result and
      // exit by itself well before that timeout, and must not exit while the retry is still waiting.
      const sdk = new URL("../src/index.js", import.meta.url).href;
      const script = `
        import { Client } from ${JSON.stringify(sdk)};
        const cl = new Client({ apiKey: "spk_test", apiUrl: ${JSON.stringify(apiUrl)}, timeoutMs: 60000 });
        const res = await cl.recordAction("c", "s");
        console.log(JSON.stringify(res));
      `;
      const started = Date.now();
      const out = await new Promise<{ code: number | null; stdout: string; stderr: string }>((resolve) => {
        const child = execFile(process.execPath, ["--input-type=module", "-e", script], { timeout: 30_000 }, (_err, stdout, stderr) =>
          resolve({ code: child.exitCode, stdout, stderr }),
        );
      });
      assert.equal(out.code, 0, out.stderr);
      assert.deepEqual(JSON.parse(out.stdout), recorded);
      assert.equal(all.length, 2);
      assert.ok(Date.now() - started < 20_000, "the process outlived the call");
    },
  );
});
