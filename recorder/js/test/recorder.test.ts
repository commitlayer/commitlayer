import { test, before, after, type TestContext } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import type { AddressInfo } from "node:net";

import { parseMapping, Recorder, type Json, type RecorderOptions } from "../src/index.js";

// The compiled CLI and the fake downstream server. The test suite is compiled to dist-test/test/, so the CLI
// is dist-test/src/cli.js and the fake server stays where it is under test/.
const CLI = new URL("../src/cli.js", import.meta.url).pathname;
const FAKE_SERVER = new URL("../../test/fake-server.mjs", import.meta.url).pathname;
const EXAMPLE_MAPPING = new URL("../../examples/mapping.json", import.meta.url).pathname;

// ---------------- a fake CommitLayer API ----------------

interface Captured {
  headers: Record<string, string | string[] | undefined>;
  body: Json;
  url: string;
  method: string;
}

const api = {
  server: undefined as Server | undefined,
  url: "",
  calls: [] as Captured[],
  mode: "ok" as "ok" | "fail" | "hang",
  /** Answers given before `mode` applies, one per request: the API's refusals, with or without Retry-After. */
  queue: [] as { status: number; code: string; retryAfter?: string }[],
};

before(async () => {
  api.server = createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk: Buffer) => (raw += chunk.toString()));
    req.on("end", () => {
      api.calls.push({ headers: req.headers, body: JSON.parse(raw) as Json, url: req.url ?? "", method: req.method ?? "" });
      const queued = api.queue.shift();
      if (queued) {
        const headers: Record<string, string> = { "content-type": "application/json" };
        if (queued.retryAfter !== undefined) headers["retry-after"] = queued.retryAfter;
        res.writeHead(queued.status, headers);
        res.end(JSON.stringify({ error: { code: queued.code, message: "the request was not applied" } }));
        return;
      }
      if (api.mode === "hang") return; // never answer: the recorder's timeout must fire
      if (api.mode === "fail") {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { code: "internal_error", message: "store unavailable" } }));
        return;
      }
      res.writeHead(201, { "content-type": "application/json" });
      res.end(JSON.stringify({ action: { action_id: (api.calls.at(-1)!.body as Json).action_id }, verification: {} }));
    });
  });
  await new Promise<void>((resolve) => api.server!.listen(0, "127.0.0.1", resolve));
  api.url = `http://127.0.0.1:${(api.server.address() as AddressInfo).port}`;
});

after(() => {
  api.server?.close();
  api.server?.closeAllConnections();
});

// ---------------- the proxy under test ----------------

class Proxy {
  readonly child: ChildProcessByStdio<Writable, Readable, Readable>;
  readonly stdout: string[] = [];
  stderr = "";
  private waiters: ((line: string) => void)[] = [];
  private readonly exited: Promise<number>;

  constructor(args: string[], env: Record<string, string | undefined>) {
    const cleanEnv: Record<string, string> = {};
    for (const [k, v] of Object.entries({ ...process.env, ...env })) if (v !== undefined) cleanEnv[k] = v;
    this.child = spawn(process.execPath, [CLI, ...args], { stdio: ["pipe", "pipe", "pipe"], env: cleanEnv });
    createInterface({ input: this.child.stdout, crlfDelay: Infinity }).on("line", (line) => {
      const w = this.waiters.shift();
      if (w) w(line);
      else this.stdout.push(line);
    });
    this.child.stderr.on("data", (d: Buffer) => (this.stderr += d.toString()));
    this.exited = new Promise((resolve) => this.child.once("close", (code) => resolve(code ?? -1)));
  }

  send(msg: Json | string): void {
    this.child.stdin.write((typeof msg === "string" ? msg : JSON.stringify(msg)) + "\n");
  }

  /** The next stdout line, as text. */
  nextLine(): Promise<string> {
    if (this.stdout.length) return Promise.resolve(this.stdout.shift()!);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  async next(): Promise<Json> {
    return JSON.parse(await this.nextLine()) as Json;
  }

  /** Ask the fake server how many tools/call it has received. */
  async downstreamCalls(id: number): Promise<number> {
    this.send({ jsonrpc: "2.0", id, method: "test/calls" });
    const r = await this.next();
    assert.equal(r.id, id);
    return (r.result as { calls: number }).calls;
  }

  async close(): Promise<number> {
    this.child.stdin.end();
    return this.exited;
  }

  warnings(): string[] {
    return this.stderr.split("\n").filter((l) => l.startsWith("commitlayer-recorder:"));
  }
}

async function waitFor(pred: () => boolean, what: string, ms = 3000): Promise<void> {
  const until = Date.now() + ms;
  while (!pred()) {
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}

const KEY = "spk_test_not_a_real_key";
const baseEnv = (extra: Record<string, string | undefined> = {}) => ({
  COMMITLAYER_API_KEY: KEY,
  COMMITLAYER_API_URL: api.url,
  COMMITLAYER_RECORDER_MAP: undefined,
  ...extra,
});

function proxy(flags: string[] = [], env: Record<string, string | undefined> = {}, mapping = EXAMPLE_MAPPING): Proxy {
  return new Proxy(["--map", mapping, ...flags, "--", process.execPath, FAKE_SERVER], baseEnv(env));
}

const initialize = { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "t", version: "0" } } };
const initialized = { jsonrpc: "2.0", method: "notifications/initialized" };
const toolsList = { jsonrpc: "2.0", id: 2, method: "tools/list" };

function refundCall(id: number, args: Json): Json {
  return { jsonrpc: "2.0", id, method: "tools/call", params: { name: "refund_payment", arguments: args } };
}

const dir = mkdtempSync(join(tmpdir(), "commitlayer-recorder-cli-"));

test.beforeEach(() => {
  api.calls = [];
  api.mode = "ok";
  api.queue = [];
});

// ---------------- passthrough ----------------

test("initialize, tools/list and non-JSON lines pass through unchanged and in order", async () => {
  const p = proxy();
  p.send(initialize);
  p.send(initialized);
  p.send(toolsList);
  p.send("this is not json");
  const init = await p.next();
  assert.equal(init.id, 1);
  assert.equal((init.result as Json).protocolVersion, "2025-06-18");
  const list = await p.next();
  assert.equal(list.id, 2);
  assert.deepEqual(
    ((list.result as Json).tools as Json[]).map((t) => t.name),
    ["refund_payment", "lookup_order"],
  );
  assert.equal(await p.nextLine(), "this is not json");
  assert.equal(await p.close(), 0);
  assert.equal(api.calls.length, 0);
  assert.deepEqual(p.warnings(), []);
});

test("the child's exit code is propagated", async () => {
  const p = proxy();
  p.send({ jsonrpc: "2.0", id: 1, method: "test/exit", params: { code: 3 } });
  assert.equal(await p.close(), 3);
});

// ---------------- recording ----------------

test("a mapped tools/call is recorded once, before it is forwarded, and the result still reaches the client", async () => {
  const p = proxy();
  p.send(initialize);
  await p.next();
  const before = Date.now();
  p.send(refundCall(5, { order_id: "ord_1", amount: 180, currency: "EUR", note: "customer asked" }));
  const r = await p.next();
  assert.equal(r.id, 5);
  const result = r.result as Json;
  assert.equal(result.isError, false);
  assert.deepEqual(JSON.parse(((result.content as Json[])[0] as { text: string }).text), {
    tool: "refund_payment",
    arguments: { order_id: "ord_1", amount: 180, currency: "EUR", note: "customer asked" },
  });

  assert.equal(api.calls.length, 1);
  const call = api.calls[0];
  assert.equal(call.method, "POST");
  assert.equal(call.url, "/v1/actions");
  assert.equal(call.headers["x-api-key"], KEY);
  assert.match(String(call.headers["content-type"]), /^application\/json/);
  const body = call.body;
  assert.deepEqual(Object.keys(body).sort(), [
    "action_id",
    "action_type",
    "agent_id",
    "channel",
    "compensates",
    "contract_id",
    "params",
    "requested_at",
    "subject_id",
    "subject_kind",
    "workflow_id",
  ].filter((k) => k !== "compensates"));
  assert.equal(body.action_id, "refund:ord_1");
  assert.equal(body.contract_id, "refund_workflow");
  assert.equal(body.action_type, "refund_order");
  assert.equal(body.subject_id, "ord_1");
  assert.equal(body.subject_kind, "order");
  assert.equal(body.agent_id, "support-bot");
  assert.equal(body.workflow_id, "refunds");
  assert.equal(body.channel, "mcp");
  assert.deepEqual(body.params, { amount: 180, currency: "EUR", mcp_tool: "refund_payment", mcp_request_id: "5" });
  assert.match(body.requested_at as string, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  const at = Date.parse(body.requested_at as string);
  assert.ok(at >= before - 1000 && at <= Date.now() + 1000, "requested_at is now");

  // The downstream result is only logged, never sent to the API.
  await waitFor(() => p.stderr.includes("recorded refund:ord_1"), "the result log line");
  assert.deepEqual(p.warnings(), ["commitlayer-recorder: recorded refund:ord_1 → tool result isError=false"]);
  assert.equal(api.calls.length, 1);
  assert.ok(!p.stderr.includes(KEY), "the key is never logged");
  await p.close();
});

test("a tool result with isError is logged as such and the record is untouched", async () => {
  const p = proxy();
  p.send(refundCall(6, { order_id: "ord_2", fail: true }));
  const r = await p.next();
  assert.equal((r.result as Json).isError, true);
  await waitFor(() => p.stderr.includes("isError=true"), "the result log line");
  assert.equal(api.calls.length, 1);
  assert.equal(api.calls[0].body.action_id, "refund:ord_2");
  await p.close();
});

test("CLI flags override agent_id and workflow_id from the mapping", async () => {
  const p = proxy(["--agent-id", "cli-agent", "--workflow-id", "wf-cli"]);
  p.send(refundCall(1, { order_id: "o" }));
  await p.next();
  assert.equal(api.calls[0].body.agent_id, "cli-agent");
  assert.equal(api.calls[0].body.workflow_id, "wf-cli");
  await p.close();
});

test("compensates is carried when its path is present", async () => {
  const p = proxy();
  p.send(refundCall(1, { order_id: "ord_3", original_action_id: "charge:ord_3" }));
  await p.next();
  assert.equal(api.calls.length, 1);
  assert.equal(api.calls[0].body.compensates, "charge:ord_3");
  await p.close();
});

test("a mapping key with a server prefix matches the bare tool name", async () => {
  const p = proxy();
  p.send({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "close_ticket", arguments: { ticket_id: "t9", reason: "solved" } } });
  await p.next();
  assert.equal(api.calls.length, 1);
  assert.equal(api.calls[0].body.contract_id, "ticket_closure");
  assert.equal(api.calls[0].body.subject_id, "t9");
  assert.equal(api.calls[0].body.action_id, "mcp:close_ticket:1");
  assert.deepEqual(api.calls[0].body.params, { reason: "solved", mcp_tool: "close_ticket", mcp_request_id: "1" });
  await p.close();
});

test("an unmapped tool is forwarded without recording; --warn-unmapped logs once per name", async () => {
  const p = proxy(["--warn-unmapped"]);
  const lookup = (id: number) => ({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "lookup_order", arguments: { order_id: "o" } } });
  p.send(lookup(1));
  p.send(lookup(2));
  assert.equal((await p.next()).id, 1);
  assert.equal((await p.next()).id, 2);
  assert.equal(api.calls.length, 0);
  assert.equal(await p.downstreamCalls(3), 2);
  await p.close();
  assert.deepEqual(p.warnings(), ["commitlayer-recorder: tool lookup_order is not mapped: forwarded without recording"]);

  const quiet = proxy();
  quiet.send(lookup(1));
  await quiet.next();
  await quiet.close();
  assert.deepEqual(quiet.warnings(), []);
});

test("a missing subject path is forwarded without recording, with a warning", async () => {
  const p = proxy();
  p.send(refundCall(4, { amount: 1 }));
  const r = await p.next();
  assert.equal(r.id, 4);
  assert.equal(api.calls.length, 0);
  assert.equal(await p.downstreamCalls(5), 1);
  await p.close();
  assert.deepEqual(p.warnings(), [
    'commitlayer-recorder: tool refund_payment: subject path "arguments.order_id" is missing from the call: forwarded without recording',
  ]);
});

// ---------------- failure modes ----------------

test("API 500 with fail-open (default) still forwards, with a warning", async () => {
  api.mode = "fail";
  const p = proxy();
  p.send(refundCall(7, { order_id: "ord_5" }));
  const r = await p.next();
  assert.equal(r.id, 7);
  assert.ok("result" in r);
  assert.equal(api.calls.length, 1);
  assert.equal(await p.downstreamCalls(8), 1);
  await p.close();
  assert.deepEqual(p.warnings(), [
    "commitlayer-recorder: recording failed for refund:ord_5 (refund_payment): HTTP 500 internal_error: store unavailable: forwarded anyway",
  ]);
});

test("API timeout with fail-open still forwards", async () => {
  api.mode = "hang";
  const p = proxy(["--timeout", "0.2"]);
  p.send(refundCall(1, { order_id: "ord_t" }));
  const r = await p.next();
  assert.ok("result" in r);
  await p.close();
  assert.deepEqual(p.warnings(), [
    "commitlayer-recorder: recording failed for refund:ord_t (refund_payment): timeout after 200ms: forwarded anyway",
  ]);
});

test("--no-fail-open with API 500 answers a JSON-RPC error and does not forward", async () => {
  api.mode = "fail";
  const p = proxy(["--no-fail-open"]);
  p.send(refundCall(9, { order_id: "ord_6" }));
  const r = await p.next();
  assert.deepEqual(r, {
    jsonrpc: "2.0",
    id: 9,
    error: { code: -32000, message: "CommitLayer recording failed: HTTP 500 internal_error: store unavailable" },
  });
  assert.equal(api.calls.length, 1);
  assert.equal(await p.downstreamCalls(10), 0);

  // Later traffic still flows, and a successful record forwards as usual.
  api.mode = "ok";
  p.send(refundCall(11, { order_id: "ord_7" }));
  const ok = await p.next();
  assert.equal(ok.id, 11);
  assert.ok("result" in ok);
  assert.equal(await p.downstreamCalls(12), 1);
  await p.close();
});

test("without an API key the recorder warns once and is a pure passthrough", async () => {
  const p = proxy([], { COMMITLAYER_API_KEY: undefined });
  p.send(initialize);
  await p.next();
  p.send(refundCall(2, { order_id: "ord_8" }));
  p.send(refundCall(3, { order_id: "ord_9" }));
  assert.equal((await p.next()).id, 2);
  assert.equal((await p.next()).id, 3);
  assert.equal(api.calls.length, 0);
  assert.equal(await p.downstreamCalls(4), 2);
  await p.close();
  assert.deepEqual(p.warnings(), [
    "commitlayer-recorder: COMMITLAYER_API_KEY is not set: running as a passthrough, nothing is recorded",
  ]);
});

test("ordering: lines queue behind a record in flight", async () => {
  const p = proxy();
  for (let i = 1; i <= 5; i++) p.send(refundCall(i, { order_id: `ord_${i}` }));
  for (let i = 1; i <= 5; i++) assert.equal((await p.next()).id, i);
  assert.deepEqual(
    api.calls.map((c) => c.body.action_id),
    ["refund:ord_1", "refund:ord_2", "refund:ord_3", "refund:ord_4", "refund:ord_5"],
  );
  await p.close();
});

// ---------------- CLI argument handling ----------------

test("--map defaults to COMMITLAYER_RECORDER_MAP", async () => {
  const p = new Proxy(["--", process.execPath, FAKE_SERVER], baseEnv({ COMMITLAYER_RECORDER_MAP: EXAMPLE_MAPPING }));
  p.send(refundCall(1, { order_id: "o" }));
  await p.next();
  assert.equal(api.calls.length, 1);
  await p.close();
});

test("an invalid mapping file is refused with a clear error and exit code 2", async () => {
  const file = join(dir, "broken.json");
  writeFileSync(file, JSON.stringify({ tools: { refund_payment: { subject: "arguments.order_id" } } }));
  const p = new Proxy(["--map", file, "--", process.execPath, FAKE_SERVER], baseEnv());
  assert.equal(await p.close(), 2);
  assert.match(p.stderr, /commitlayer-recorder: .*broken\.json: tools\.refund_payment\.contract_id is required/);
});

test("a missing downstream command is refused with exit code 2", async () => {
  const p = new Proxy(["--map", EXAMPLE_MAPPING], baseEnv());
  assert.equal(await p.close(), 2);
  assert.match(p.stderr, /missing "--" before the downstream command/);
});

test("a downstream command that cannot start exits 127", async () => {
  const p = new Proxy(["--map", EXAMPLE_MAPPING, "--", "/nonexistent/mcp-server"], baseEnv());
  assert.equal(await p.close(), 127);
  assert.match(p.stderr, /commitlayer-recorder: cannot start \/nonexistent\/mcp-server/);
});

// ---------------- retry on 429 and 503 with Retry-After ----------------

/**
 * A proxy that is stopped when the test ends, whatever the outcome, and a time limit for the test: a failed
 * assertion or a line that never comes is then reported, instead of leaving the child running.
 */
const LIMIT = { timeout: 30_000 };

function guarded(t: TestContext, flags: string[] = []): Proxy {
  const p = proxy(flags);
  t.after(() => void p.child.kill());
  return p;
}

test("a 429 workspace_busy with Retry-After is recorded on the retry, same body, before the call is forwarded", LIMIT, async (t) => {
  api.queue = [{ status: 429, code: "workspace_busy", retryAfter: "0.05" }];
  const p = guarded(t);
  p.send(refundCall(7, { order_id: "ord_r1", amount: 180 }));
  const r = await p.next();
  assert.equal(r.id, 7);
  assert.ok("result" in r);
  assert.equal(api.calls.length, 2);
  assert.deepEqual(api.calls[1].body, api.calls[0].body); // the same action_id, requested_at and params
  assert.equal(api.calls[0].body.action_id, "refund:ord_r1");
  assert.equal(await p.downstreamCalls(8), 1);
  await p.close();
  const w = p.warnings();
  assert.equal(w.length, 2);
  assert.match(
    w[0],
    /^commitlayer-recorder: recording refund:ord_r1 \(refund_payment\) answered HTTP 429 workspace_busy: the request was not applied: retry 1 of 2 in \d+ ms$/,
  );
  assert.equal(w[1], "commitlayer-recorder: recorded refund:ord_r1 → tool result isError=false");
  assert.ok(!p.stderr.includes(KEY));
});

test("a 503 busy with Retry-After is retried too; past the limit the last answer is the failure", LIMIT, async (t) => {
  api.queue = [
    { status: 503, code: "busy", retryAfter: "0.01" },
    { status: 429, code: "workspace_busy", retryAfter: "0.01" },
    { status: 503, code: "busy", retryAfter: "0.01" },
  ];
  const p = guarded(t);
  p.send(refundCall(1, { order_id: "ord_r2" }));
  const r = await p.next();
  assert.ok("result" in r); // fail-open: forwarded all the same
  assert.equal(api.calls.length, 3); // the first attempt and the two retries
  assert.equal(new Set(api.calls.map((c) => JSON.stringify(c.body))).size, 1);
  await p.close();
  const w = p.warnings();
  assert.equal(w.length, 3);
  assert.match(w[0], /answered HTTP 503 busy: the request was not applied: retry 1 of 2 in \d+ ms$/);
  assert.match(w[1], /answered HTTP 429 workspace_busy: the request was not applied: retry 2 of 2 in \d+ ms$/);
  assert.equal(
    w[2],
    "commitlayer-recorder: recording failed for refund:ord_r2 (refund_payment): HTTP 503 busy: the request was not applied (after 3 attempts): forwarded anyway",
  );
});

test("--retries 0 turns the retry off; --no-fail-open then refuses the call on the first 429", LIMIT, async (t) => {
  api.queue = [{ status: 429, code: "workspace_busy", retryAfter: "0.01" }];
  const p = guarded(t, ["--retries", "0", "--no-fail-open"]);
  p.send(refundCall(9, { order_id: "ord_r3" }));
  const r = await p.next();
  assert.deepEqual(r, {
    jsonrpc: "2.0",
    id: 9,
    error: { code: -32000, message: "CommitLayer recording failed: HTTP 429 workspace_busy: the request was not applied" },
  });
  assert.equal(api.calls.length, 1);
  assert.equal(await p.downstreamCalls(10), 0);
  await p.close();
});

test("--retries must be a whole number", LIMIT, async (t) => {
  for (const bad of ["-1", "1.5", "many"]) {
    const p = guarded(t, ["--retries", bad]);
    assert.equal(await p.close(), 2);
    assert.match(p.stderr, /--retries must be a whole number, 0 or more/);
  }
});

test("a 500, a 409, and a 429 or 503 without Retry-After are not retried", LIMIT, async (t) => {
  const refusals = [
    { status: 500, code: "internal_error", retryAfter: "0.01" },
    { status: 409, code: "conflict", retryAfter: "0.01" },
    { status: 400, code: "invalid_request", retryAfter: "0.01" },
    { status: 502, code: "bad_gateway", retryAfter: "0.01" },
    { status: 504, code: "gateway_timeout", retryAfter: "0.01" },
    { status: 429, code: "rate_limited" },
    { status: 503, code: "busy" },
    { status: 429, code: "workspace_busy", retryAfter: "soon" },
    { status: 429, code: "workspace_busy", retryAfter: "-1" },
  ];
  const p = guarded(t);
  let id = 20;
  for (const refusal of refusals) {
    api.calls = [];
    api.queue = [refusal];
    p.send(refundCall(id, { order_id: `ord_n${id}` }));
    const r = await p.next();
    assert.ok("result" in r);
    assert.equal(api.calls.length, 1, `${refusal.status} ${refusal.code} ${refusal.retryAfter ?? "(no header)"}`);
    id += 1;
  }
  await p.close();
  const w = p.warnings().filter((l) => l.includes("recording"));
  assert.equal(w.length, refusals.length);
  assert.ok(w.every((l) => l.endsWith(": forwarded anyway") && !l.includes("attempts") && !l.includes("retry")));
});

test("a wait that does not fit in the record's time is not made", LIMIT, async (t) => {
  api.queue = [{ status: 429, code: "workspace_busy", retryAfter: "1" }];
  const p = guarded(t, ["--timeout", "0.5"]);
  const started = Date.now();
  p.send(refundCall(1, { order_id: "ord_r4" }));
  const r = await p.next();
  assert.ok("result" in r);
  assert.equal(api.calls.length, 1);
  await p.close();
  assert.deepEqual(p.warnings().slice(0, 1), [
    "commitlayer-recorder: recording failed for refund:ord_r4 (refund_payment): HTTP 429 workspace_busy: the request was not applied: forwarded anyway",
  ]);
  assert.ok(Date.now() - started < 5000);
});

test("the timeout covers the whole record: a retry that is never answered ends at the same deadline", LIMIT, async (t) => {
  api.queue = [{ status: 503, code: "busy", retryAfter: "0.05" }];
  api.mode = "hang";
  const p = guarded(t, ["--timeout", "1.3"]); // the 50 ms wait and a second for the retry fit in it
  p.send(refundCall(1, { order_id: "ord_r5" }));
  const r = await p.next();
  assert.ok("result" in r);
  assert.equal(api.calls.length, 2);
  await p.close();
  const w = p.warnings();
  assert.equal(w.length, 2);
  assert.match(w[0], /answered HTTP 503 busy: the request was not applied: retry 1 of 2 in \d+ ms$/);
  assert.equal(
    w[1],
    "commitlayer-recorder: recording failed for refund:ord_r5 (refund_payment): timeout after 1300ms: forwarded anyway",
  );
});

// The Recorder class with a fake fetch, wait and random source: the exact waits, without sleeping.

function unit(replies: { status: number; retryAfter?: string }[], opts: Partial<RecorderOptions> = {}) {
  const sent: string[] = [];
  const sleeps: number[] = [];
  const logs: string[] = [];
  const fakeFetch = (async (_url: RequestInfo | URL, init?: RequestInit) => {
    sent.push(String(init?.body));
    const reply = replies[Math.min(sent.length - 1, replies.length - 1)];
    const headers = new Headers({ "content-type": "application/json" });
    if (reply.retryAfter !== undefined) headers.set("Retry-After", reply.retryAfter);
    const body = reply.status < 400 ? { action: {} } : { error: { code: reply.status === 429 ? "workspace_busy" : "busy", message: "not applied" } };
    return new Response(JSON.stringify(body), { status: reply.status, headers });
  }) as typeof fetch;
  const rec = new Recorder({
    mapping: parseMapping({ tools: { refund_payment: { contract_id: "refund_workflow", subject: "arguments.order_id" } } }),
    apiKey: KEY,
    apiUrl: "http://api.invalid",
    fetch: fakeFetch,
    sleep: async (ms) => void sleeps.push(ms),
    random: () => 0,
    log: (m) => logs.push(m),
    ...opts,
  });
  const line = JSON.stringify({ jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "refund_payment", arguments: { order_id: "o1" } } });
  return { rec, sent, sleeps, logs, line };
}

test("Recorder: the default is 2 retries; the wait is the stated second and the bytes are the same", async () => {
  const u = unit([{ status: 429, retryAfter: "1" }, { status: 201 }]);
  assert.equal(u.rec.maxRetries, 2);
  assert.deepEqual(await u.rec.intercept(u.line), { forward: true });
  assert.equal(u.sent.length, 2);
  assert.equal(u.sent[1], u.sent[0]);
  assert.equal((JSON.parse(u.sent[0]) as Json).action_id, "mcp:refund_payment:5");
  assert.deepEqual(u.sleeps, [1000]);
  assert.deepEqual(u.logs, [
    "recording mcp:refund_payment:5 (refund_payment) answered HTTP 429 workspace_busy: not applied: retry 1 of 2 in 1000 ms",
  ]);
  assert.ok(!u.logs.join("\n").includes(KEY));
});

test("Recorder: a call without a JSON-RPC id gets one generated action_id for every attempt", async () => {
  const u = unit([{ status: 503, retryAfter: "1" }, { status: 429, retryAfter: "1" }, { status: 201 }]);
  const line = JSON.stringify({ jsonrpc: "2.0", method: "tools/call", params: { name: "refund_payment", arguments: { order_id: "o1" } } });
  assert.deepEqual(await u.rec.intercept(line), { forward: true });
  assert.equal(u.sent.length, 3);
  assert.equal(new Set(u.sent).size, 1);
  assert.match((JSON.parse(u.sent[0]) as Json).action_id as string, /^mcp:refund_payment:[0-9a-f-]{36}$/);
});

test("Recorder: the random addition is at most twice the stated wait, and a share of what the record's time allows", async () => {
  const none = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { random: () => 0 });
  await none.rec.intercept(none.line);
  assert.deepEqual(none.sleeps, [1000]); // never less than stated

  const half = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { random: () => 0.5 });
  await half.rec.intercept(half.line);
  assert.deepEqual(half.sleeps, [2000]); // 5 s for the record: the whole of the two seconds is there to share

  const most = unit([{ status: 429, retryAfter: "0.5" }, { status: 201 }], { random: () => 0.999 });
  await most.rec.intercept(most.line);
  assert.deepEqual(most.sleeps, [1499]); // just under three times the stated half second

  // 2.5 s for the record: after the stated second and the second kept for the retry, half a second is left
  // to share, so the wait ends in time for the attempt after it.
  const tight = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { random: () => 0.999, timeoutMs: 2500 });
  await tight.rec.intercept(tight.line);
  assert.equal(tight.sent.length, 2);
  assert.ok(tight.sleeps[0] >= 1400 && tight.sleeps[0] < 1500, `waited ${tight.sleeps[0]} ms`);
});

test("Recorder: a wait is made only when a second is left for the attempt after it", async () => {
  // 2 s for the record and one second stated: nothing would be left for the retry, so the record is sent once.
  const none = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { timeoutMs: 2000, failOpen: false });
  const d = await none.rec.intercept(none.line);
  assert.equal(none.sent.length, 1);
  assert.deepEqual(none.sleeps, []);
  assert.match((d as { reply: string }).reply, /CommitLayer recording failed: HTTP 429 workspace_busy: not applied"/);

  // 2.2 s: the second fits, with room for the attempt.
  const some = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { timeoutMs: 2200 });
  await some.rec.intercept(some.line);
  assert.equal(some.sent.length, 2);
  assert.deepEqual(some.sleeps, [1000]);
});

test("Recorder: past maxRetries, or with a wait longer than the time left, the refusal is the failure", async () => {
  const used = unit([{ status: 429, retryAfter: "1" }], { maxRetries: 1, failOpen: false });
  const d = await used.rec.intercept(used.line);
  assert.equal(d.forward, false);
  assert.equal(used.sent.length, 2);
  assert.deepEqual(used.sleeps, [1000]);
  assert.match((d as { reply: string }).reply, /HTTP 429 workspace_busy: not applied \(after 2 attempts\)/);

  const long = unit([{ status: 503, retryAfter: "5" }, { status: 201 }]); // the default record time is 5 s
  await long.rec.intercept(long.line);
  assert.equal(long.sent.length, 1);
  assert.deepEqual(long.sleeps, []);
  assert.deepEqual(long.logs, [
    "recording failed for mcp:refund_payment:5 (refund_payment): HTTP 503 busy: not applied: forwarded anyway",
  ]);

  const off = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { maxRetries: 0 });
  await off.rec.intercept(off.line);
  assert.equal(off.sent.length, 1);
  assert.deepEqual(off.sleeps, []);

  for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => unit([{ status: 201 }], { maxRetries: bad }), /maxRetries must be zero or more/);
  }
});

test("Recorder: an HTTP-date Retry-After, and values that are not a wait", async () => {
  const at = new Date(Math.ceil(Date.now() / 1000) * 1000 + 2000).toUTCString();
  const dated = unit([{ status: 503, retryAfter: at }, { status: 201 }]);
  await dated.rec.intercept(dated.line);
  assert.equal(dated.sent.length, 2);
  assert.equal(dated.sleeps.length, 1);
  assert.ok(dated.sleeps[0] > 1000 && dated.sleeps[0] <= 3000, `waited ${dated.sleeps[0]} ms`);

  for (const header of ["", "soon", "-1", "1e3", "1, 1", "NaN", "2026-10-21T07:28:00Z", "99999999999", "9".repeat(400)]) {
    const u = unit([{ status: 429, retryAfter: header }, { status: 201 }]);
    await u.rec.intercept(u.line);
    assert.equal(u.sent.length, 1, `Retry-After ${JSON.stringify(header.slice(0, 30))}`);
    assert.deepEqual(u.sleeps, []);
  }

  // A date already passed is a retry now, never a negative wait.
  const past = unit([{ status: 503, retryAfter: new Date(Date.now() - 60_000).toUTCString() }, { status: 201 }]);
  await past.rec.intercept(past.line);
  assert.equal(past.sent.length, 2);
  assert.deepEqual(past.sleeps, [0]);

  // Decimal seconds are rounded up to a whole millisecond: never shorter than stated.
  for (const [header, ms] of [["0.0015", 2], ["0.5", 500], ["2.007", 2007]] as [string, number][]) {
    const u = unit([{ status: 429, retryAfter: header }, { status: 201 }]);
    await u.rec.intercept(u.line);
    assert.deepEqual(u.sleeps, [ms], `Retry-After ${header}`);
  }
});

test("Recorder: only 429 and 503 are retried, whatever else carries Retry-After", async () => {
  for (const status of [400, 401, 402, 403, 404, 408, 409, 422, 425, 500, 501, 502, 504]) {
    const u = unit([{ status, retryAfter: "1" }, { status: 201 }]);
    await u.rec.intercept(u.line);
    assert.equal(u.sent.length, 1, `status ${status}`);
    assert.deepEqual(u.sleeps, []);
  }
});

test("Recorder: a random source out of range adds nothing, and maxRetries is a whole number", async () => {
  for (const r of [5, -1, Number.NaN]) {
    const u = unit([{ status: 429, retryAfter: "1" }, { status: 201 }], { random: () => r });
    await u.rec.intercept(u.line);
    assert.deepEqual(u.sleeps, [1000], `random ${r}`);
  }
  const u = unit([{ status: 429, retryAfter: "1" }], { maxRetries: 1.9 });
  assert.equal(u.rec.maxRetries, 1);
  await u.rec.intercept(u.line);
  assert.equal(u.sent.length, 2);
});

/** A `fetch` written by hand, as a test of the caller's own may have one: it answers with whatever it is given. */
function doubleFetch(answers: { status: number; headers?: unknown }[], sent: string[]): typeof fetch {
  return (async (_url: RequestInfo | URL, init?: RequestInit) => {
    sent.push(String(init?.body));
    const a = answers[Math.min(sent.length - 1, answers.length - 1)];
    const text = async () => (a.status < 400 ? "{}" : JSON.stringify({ error: { code: "busy", message: "not applied" } }));
    return { status: a.status, text, ...(a.headers === undefined ? {} : { headers: a.headers }) };
  }) as unknown as typeof fetch;
}

test("Recorder: a fetch double that answers without headers fails the record with the API's answer, not a TypeError", async () => {
  for (const headers of [undefined, {}]) {
    const sent: string[] = [];
    const u = unit([], { fetch: doubleFetch([{ status: 503, headers }, { status: 201 }], sent) });
    assert.deepEqual(await u.rec.intercept(u.line), { forward: true });
    assert.equal(sent.length, 1);
    assert.deepEqual(u.logs, [
      "recording failed for mcp:refund_payment:5 (refund_payment): HTTP 503 busy: not applied: forwarded anyway",
    ]);
  }

  // The header as such a double may give it, with the spaces a real fetch would have removed.
  const sent: string[] = [];
  const spaced = unit([], { fetch: doubleFetch([{ status: 503, headers: { get: () => " 1 " } }, { status: 201 }], sent) });
  await spaced.rec.intercept(spaced.line);
  assert.equal(sent.length, 2);
  assert.deepEqual(spaced.sleeps, [1000]);
});

test("Recorder: the default wait is a real timer that runs for the stated time", async () => {
  const u = unit([{ status: 429, retryAfter: "0.3" }, { status: 201 }], { sleep: undefined });
  const started = performance.now();
  assert.deepEqual(await u.rec.intercept(u.line), { forward: true });
  const elapsed = performance.now() - started;
  assert.equal(u.sent.length, 2);
  assert.ok(elapsed >= 299, `the retry came after ${elapsed} ms, before the stated 300`);
  assert.ok(elapsed < 2000, `the retry came after ${elapsed} ms`);
});

test("Recorder: one deadline for the whole record, measured: a retry that is never answered does not start a new timeout", async () => {
  // 1.4 s for the record. The first answer is a 503 that asks for 300 ms; the retry is never answered. The
  // record must end 1.4 s after it began: a timeout that started again with the retry would end after 1.7 s.
  let requests = 0;
  const hanging = ((_url: RequestInfo | URL, init?: RequestInit) => {
    requests += 1;
    if (requests === 1) {
      const body = JSON.stringify({ error: { code: "busy", message: "not applied" } });
      return Promise.resolve(new Response(body, { status: 503, headers: { "Retry-After": "0.3" } }));
    }
    return new Promise<Response>((_resolve, reject) => {
      init!.signal!.addEventListener("abort", () => reject(new DOMException("This operation was aborted", "AbortError")));
    });
  }) as typeof fetch;
  const u = unit([], { fetch: hanging, sleep: undefined, random: () => 0, timeoutMs: 1400, failOpen: false });
  const started = performance.now();
  const d = await u.rec.intercept(u.line);
  const elapsed = performance.now() - started;
  assert.equal(requests, 2);
  assert.equal(d.forward, false);
  assert.match((d as { reply: string }).reply, /CommitLayer recording failed: timeout after 1400ms/);
  assert.ok(elapsed >= 1399, `the record ended after ${elapsed} ms, before its 1400`);
  assert.ok(elapsed < 1650, `the record was held for ${elapsed} ms: the retry got a timeout of its own`);
});
