// The manifest in scripts/api_surface.py says what the TypeScript packages do: the bodies the SDK and the recorder
// send, and the fields the integrations' verdict reader takes from an answer. Runs against the built packages
// (`npm run build` in sdk/js, recorder/js and integrations/js first):
//
//   node --test scripts/tests/surface.test.mjs

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const inventory = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["scripts/api_surface.py"], { cwd: ROOT, encoding: "utf8" }));
const uses = (pkg) => inventory.packages.find((p) => p.package === pkg).uses;
const norm = (p) => p.split("?")[0].replace(/\{[^}]*\}/g, "{}");
const matches = (template, path) => {
  const t = norm(template).split("/");
  const p = path.split("?")[0].split("/");
  return t.length === p.length && t.every((s, i) => s === "{}" || s === p[i]);
};
const useFor = (pkg, method, path) => {
  const found = uses(pkg).filter((u) => u.method === method && matches(u.path, path));
  assert.equal(found.length, 1, `${pkg}: ${method} ${path}`);
  return found[0];
};
const query = (path) => new Set((path.split("?")[1] || "").split("&").filter(Boolean).map((kv) => kv.split("=")[0]));
const sorted = (xs) => [...xs].sort();

const sdk = await import("../../sdk/js/dist/index.js");
const recorder = await import("../../recorder/js/dist/index.js");
const integrations = await import("../../integrations/js/dist/index.js");

function capture() {
  const calls = [];
  const transport = async (method, path, body) => {
    calls.push({ method, path, body });
    return { status: 200, json: { timed_out: false } };
  };
  return { calls, client: new sdk.Client({ transport, agentId: undefined }) };
}

async function exercise(full) {
  const { calls, client } = capture();
  if (full) {
    await client.recordAction("c", "s", {
      actionId: "i", params: { x: 1 }, requestedAt: "2026-01-01T00:00:00Z", actionType: "t", subjectKind: "k",
      workflowId: "w", agentId: "a", channel: "api", compensates: "o", contractVersion: 2,
    });
    await client.pushFacts([{ source: "x" }], { links: [{ a: 1 }], watermarks: { x: "t" } });
    await client.verify({ actionId: "i", action: { action_id: "j" }, facts: [{ source: "x" }], links: [{ a: 1 }], dryRun: true });
    await client.packs.dryRun("p", { w: {} }, { workflow: "w" });
  } else {
    await client.recordAction("c", "s");
    await client.pushFacts([]);
    await client.verify();
    await client.packs.dryRun("p");
  }
  await client.getAction("i");
  await client.waitForSettlement("i", { timeoutMs: 1 });
  await client.draftContract("t");
  await client.validateContract("y");
  await client.saveContract("y");
  await client.packs.list();
  await client.packs.get("p");
  await client.packs.configure("p");
  await client.packs.install("p", {}, { confirm: true });
  await client.packs.coverage("p");
  await client.packs.recorderMapping("p");
  return calls;
}

test("the TypeScript SDK sends exactly the listed fields with every option set", async () => {
  const seen = new Set();
  for (const c of await exercise(true)) {
    const u = useFor("sdk-js", c.method, c.path);
    seen.add(`${u.method} ${u.path}`);
    assert.deepEqual(sorted(Object.keys(c.body ?? {})), sorted(u.request), `${c.method} ${c.path}`);
    assert.deepEqual(sorted(query(c.path)), sorted(u.query), `${c.method} ${c.path}`);
  }
  assert.deepEqual(sorted(seen), sorted(uses("sdk-js").map((u) => `${u.method} ${u.path}`)));
});

test("the TypeScript SDK always sends the listed required fields", async () => {
  for (const c of await exercise(false)) {
    const u = useFor("sdk-js", c.method, c.path);
    assert.deepEqual(sorted(Object.keys(c.body ?? {})), sorted(u.required), `${c.method} ${c.path}`);
  }
});

test("the TypeScript recorder sends exactly the listed fields", () => {
  const u = useFor("recorder-js", "POST", "/v1/actions");
  const call = { name: "t", arguments: { id: "1", n: 2, prev: "p" } };
  const full = recorder.buildAction(
    { contract_id: "c", subject: "arguments.id", contract_version: 3, action_type: "a", subject_kind: "k", params: { n: "arguments.n" }, compensates: "arguments.prev" },
    call,
    { requestId: 1, agentId: "ag", workflowId: "wf" },
  );
  assert.deepEqual(sorted(Object.keys(full)), sorted(u.request));
  const bare = recorder.buildAction({ contract_id: "c", subject: "arguments.id" }, call, { requestId: 1 });
  assert.deepEqual(sorted(Object.keys(bare)), sorted(u.required));
});

function tracked(data, seen, prefix = "") {
  if (data === null || typeof data !== "object" || Array.isArray(data)) return data;
  return new Proxy(data, {
    get(target, key) {
      if (typeof key !== "string") return target[key];
      seen.add(prefix + key);
      return tracked(target[key], seen, `${prefix}${key}.`);
    },
  });
}
const leaves = (paths) => new Set([...paths].filter((p) => ![...paths].some((q) => q.startsWith(p + "."))));

test("the TypeScript verdict reader reads exactly the listed fields", () => {
  const seen = new Set();
  integrations.summarizeVerification(
    tracked(
      {
        action: { action_id: "a" },
        lenses: { outcome: { verdict: "PASS", reason_codes: [], reason_labels: [] }, policy: { verdict: "PASS", reason_codes: [], reason_labels: [] } },
        status: "settled",
        summary: { headline: "h" },
        settles_at: "t",
      },
      seen,
    ),
  );
  assert.deepEqual(sorted(leaves(seen)), sorted(useFor("integrations-js", "GET", "/v1/actions/x").response));
});
