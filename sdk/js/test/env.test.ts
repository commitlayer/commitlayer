import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

import { _resetDeprecationWarnings, Client, CommitLayerError, ENV, evidence, recorder, type Json, type Transport } from "../src/index.js";

const NAMES = ["COMMITLAYER_RECORDER_KEY", "COMMITLAYER_EVIDENCE_KEY", "COMMITLAYER_API_KEY", "COMMITLAYER_API_URL", "COMMITLAYER_WORKSPACE_ID", "COMMITLAYER_CONTRACT_ID"];

/** Run `fn` with exactly these CommitLayer variables set, and put the environment back after. */
function withEnv(vars: Record<string, string>, fn: () => void): void {
  const saved = Object.fromEntries(NAMES.map((n) => [n, process.env[n]]));
  for (const n of NAMES) delete process.env[n];
  Object.assign(process.env, vars);
  _resetDeprecationWarnings();
  try {
    fn();
  } finally {
    for (const n of NAMES) {
      if (saved[n] === undefined) delete process.env[n];
      else process.env[n] = saved[n];
    }
  }
}

function fake(answers: Record<string, { status: number; json: Json }> = {}) {
  const calls: { method: string; path: string; body: Json | null | undefined }[] = [];
  const transport: Transport = async (method, path, body) => {
    calls.push({ method, path, body });
    return answers[path] ?? { status: 200, json: { ok: true } };
  };
  return { calls, transport };
}

test("recorder() and evidence() read their own key and never the other", () => {
  withEnv({ COMMITLAYER_RECORDER_KEY: "rec-value", COMMITLAYER_EVIDENCE_KEY: "ev-value", COMMITLAYER_API_URL: "http://127.0.0.1:9/" }, () => {
    assert.equal(recorder().apiKey, "rec-value");
    assert.equal(evidence().apiKey, "ev-value");
    assert.equal(recorder().apiUrl, "http://127.0.0.1:9");
  });
});

test("a plain client takes the one key the process has and refuses both", () => {
  withEnv({ COMMITLAYER_EVIDENCE_KEY: "ev-only" }, () => assert.equal(new Client().apiKey, "ev-only"));
  withEnv({ COMMITLAYER_EVIDENCE_KEY: "ev", COMMITLAYER_RECORDER_KEY: "rec" }, () => assert.throws(() => new Client(), /separate processes/));
  withEnv({ COMMITLAYER_EVIDENCE_KEY: "ev-only" }, () => assert.throws(() => recorder(), /COMMITLAYER_RECORDER_KEY/));
});

test("COMMITLAYER_API_KEY still works and warns once", () => {
  withEnv({ COMMITLAYER_API_KEY: "legacy" }, () => {
    const warnings: string[] = [];
    const listener = (w: Error) => warnings.push(`${w.name}: ${w.message}`);
    process.on("warning", listener);
    try {
      assert.equal(new Client().apiKey, "legacy");
      assert.equal(recorder().apiKey, "legacy");
    } finally {
      process.off("warning", listener);
    }
  });
  assert.equal(ENV.legacyKey, "COMMITLAYER_API_KEY");
});

test("the contract and workspace come from the environment", async () => {
  const t = fake();
  let cl: Client | undefined;
  withEnv({ COMMITLAYER_CONTRACT_ID: "scop_refund_order", COMMITLAYER_WORKSPACE_ID: "ten_1" }, () => {
    cl = new Client({ transport: t.transport, role: "recorder" });
  });
  assert.equal(cl!.workspaceId, "ten_1");
  await cl!.recordAction(null, "ord_1", { actionId: "refund-ord_1" });
  assert.equal(t.calls[0].body?.contract_id, "scop_refund_order");
  await cl!.recordAction("other", "ord_2", { actionId: "x" });
  assert.equal(t.calls[1].body?.contract_id, "other");
  await assert.rejects(new Client({ transport: t.transport }).recordAction(null, "ord_3"), /COMMITLAYER_CONTRACT_ID/);
});

test("publishBatch sends records and a checkpoint for every source", async () => {
  const answer = {
    facts: [{ fact_id: "fct_1", accepted: true }],
    checkpoints: ["stripe", "shopify", "zendesk"].map((source) => ({ source, observed_through: "2026-11-18T10:00:00Z", accepted: true })),
    affected_actions: ["act_1"],
    reverify_scheduled: true,
    snapshot_versions: { act_1: 3 },
  };
  const t = fake({ "/v1/evidence/batch": { status: 201, json: answer } });
  const cl = new Client({ transport: t.transport, role: "evidence" });
  const when = new Date("2026-11-18T10:00:00.000Z");
  const fact = { source: "stripe", subject_id: "pay_1", fact_type: "refund", occurred_at: "2026-11-18T09:59:00Z" };
  const got = await cl.publishBatch({ facts: [fact], observedThrough: { stripe: when, shopify: when, zendesk: "2026-11-18T10:00:00Z" } });
  assert.deepEqual(got, answer);
  assert.deepEqual(t.calls[0], {
    method: "POST",
    path: "/v1/evidence/batch",
    body: { facts: [fact], observed_through: { stripe: "2026-11-18T10:00:00Z", shopify: "2026-11-18T10:00:00Z", zendesk: "2026-11-18T10:00:00Z" } },
  });
});

test("a checkpoint alone is a valid batch; one checkpoint is required; a recorder key is refused readably", async () => {
  const t = fake({ "/v1/evidence/batch": { status: 201, json: { facts: [], checkpoints: [] } } });
  await new Client({ transport: t.transport }).publishBatch({ observedThrough: { stripe: "2026-11-18T10:00:00Z" } });
  assert.deepEqual(t.calls, [{ method: "POST", path: "/v1/evidence/batch", body: { facts: [], observed_through: { stripe: "2026-11-18T10:00:00Z" } } }]);
  await assert.rejects(new Client({ transport: t.transport }).publishBatch({ observedThrough: {} }), /observedThrough/);
  const refused = fake({ "/v1/evidence/batch": { status: 403, json: { error: { code: "insufficient_scope", message: "This key cannot write facts." } } } });
  await assert.rejects(new Client({ transport: refused.transport, maxRetries: 0 }).publishBatch({ observedThrough: { stripe: "2026-11-18T10:00:00Z" } }), (e: unknown) => e instanceof CommitLayerError && e.status === 403 && e.code === "insufficient_scope");
});

test("the packed artifact works from require and import and reports the package version", { skip: !existsSync(new URL("../../dist/cjs/index.js", import.meta.url)) && "run `npm run build` first" }, () => {
  const root = new URL("../../", import.meta.url).pathname;
  const version = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version;
  const dir = mkdtempSync(join(tmpdir(), "commitlayer-pack-"));
  const tgz = execFileSync("npm", ["pack", "--silent", "--pack-destination", dir], { cwd: root, encoding: "utf8" }).trim().split("\n").pop()!;
  const project = join(dir, "project");
  execFileSync("mkdir", ["-p", project]);
  writeFileSync(join(project, "package.json"), JSON.stringify({ name: "probe", private: true }));
  execFileSync("npm", ["install", "--silent", "--no-audit", "--no-fund", "--offline", join(dir, tgz)], { cwd: project });
  const cjs = execFileSync("node", ["-e", "const c = require('commitlayer'); console.log(c.VERSION, typeof c.recorder, typeof c.Client)"], { cwd: project, encoding: "utf8" }).trim();
  const esm = execFileSync("node", ["--input-type=module", "-e", "import { VERSION, evidence } from 'commitlayer'; console.log(VERSION, typeof evidence)"], { cwd: project, encoding: "utf8" }).trim();
  assert.equal(cjs, `${version} function function`);
  assert.equal(esm, `${version} function`);
});
