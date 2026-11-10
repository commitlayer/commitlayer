import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import type { AddressInfo } from "node:net";

import type { Json } from "../src/index.js";

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
};

before(async () => {
  api.server = createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk: Buffer) => (raw += chunk.toString()));
    req.on("end", () => {
      api.calls.push({ headers: req.headers, body: JSON.parse(raw) as Json, url: req.url ?? "", method: req.method ?? "" });
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
