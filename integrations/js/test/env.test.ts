import { test } from "node:test";
import assert from "node:assert/strict";

import { _resetDeprecationWarnings, Client } from "commitlayer";

import { resolveClient, sdkClientOptions } from "../src/index.js";
import { commitlayerActivityInterceptors, type ActivityInfoLike } from "../src/temporal.js";

// The client an integration builds when it is given no key reads the environment through the SDK as a recorder:
// COMMITLAYER_RECORDER_KEY, then the deprecated COMMITLAYER_API_KEY with a warning, never the evidence key.

const NAMES = ["COMMITLAYER_RECORDER_KEY", "COMMITLAYER_EVIDENCE_KEY", "COMMITLAYER_API_KEY", "COMMITLAYER_API_URL"];

/** Run `fn` with exactly these CommitLayer variables set; the DeprecationWarnings it caused; the environment put back. */
async function withEnv(vars: Record<string, string>, fn: () => void | Promise<void>): Promise<string[]> {
  const saved = Object.fromEntries(NAMES.map((n) => [n, process.env[n]]));
  for (const n of NAMES) delete process.env[n];
  Object.assign(process.env, vars);
  _resetDeprecationWarnings();
  const warnings: string[] = [];
  const listener = (w: Error) => {
    if (w.name === "DeprecationWarning") warnings.push(w.message);
  };
  process.on("warning", listener);
  try {
    await fn();
    await new Promise((r) => setImmediate(r)); // process warnings are delivered on a later tick
  } finally {
    process.off("warning", listener);
    _resetDeprecationWarnings();
    for (const n of NAMES) {
      if (saved[n] === undefined) delete process.env[n];
      else process.env[n] = saved[n];
    }
  }
  return warnings;
}

const DEPRECATION =
  "COMMITLAYER_API_KEY is deprecated; set COMMITLAYER_RECORDER_KEY (and keep the recorder and evidence keys in separate processes).";

/** The key and role of the client an integration builds from the options it was given. */
function built(): { apiKey: string; role: unknown } {
  const c = resolveClient({}) as Client;
  return { apiKey: c.apiKey, role: c.role };
}

test("the client built here is a recorder", () => {
  assert.equal(sdkClientOptions({}).role, "recorder");
  assert.equal(sdkClientOptions({ apiKey: "explicit-key" }).role, "recorder");
});

test("the key is read from COMMITLAYER_RECORDER_KEY", async () => {
  const warnings = await withEnv({ COMMITLAYER_RECORDER_KEY: "rec-key" }, () => {
    assert.deepEqual(built(), { apiKey: "rec-key", role: "recorder" });
  });
  assert.deepEqual(warnings, []);
});

test("COMMITLAYER_API_KEY still works and warns once that it is deprecated", async () => {
  const warnings = await withEnv({ COMMITLAYER_API_KEY: "legacy-key" }, () => {
    assert.deepEqual(built(), { apiKey: "legacy-key", role: "recorder" });
    assert.deepEqual(built(), { apiKey: "legacy-key", role: "recorder" });
  });
  assert.deepEqual(warnings, [DEPRECATION]);
});

test("COMMITLAYER_RECORDER_KEY wins over the deprecated name", async () => {
  const warnings = await withEnv({ COMMITLAYER_RECORDER_KEY: "rec-key", COMMITLAYER_API_KEY: "legacy-key" }, () => {
    assert.deepEqual(built(), { apiKey: "rec-key", role: "recorder" });
  });
  assert.deepEqual(warnings, []);
});

test("the evidence key is never read, even when it is the only key or sits beside the recorder key", async () => {
  await withEnv({ COMMITLAYER_EVIDENCE_KEY: "evidence-key" }, () => {
    assert.throws(() => resolveClient({}), /apiKey is required \(or set COMMITLAYER_RECORDER_KEY\)/);
  });
  await withEnv({ COMMITLAYER_EVIDENCE_KEY: "evidence-key", COMMITLAYER_RECORDER_KEY: "rec-key" }, () => {
    assert.deepEqual(built(), { apiKey: "rec-key", role: "recorder" });
  });
});

test("an explicit key and URL win over the environment", async () => {
  const warnings = await withEnv(
    { COMMITLAYER_RECORDER_KEY: "rec-key", COMMITLAYER_API_KEY: "legacy-key", COMMITLAYER_API_URL: "http://env.invalid" },
    () => {
      const c = resolveClient({ apiKey: "explicit-key", apiUrl: "http://explicit.invalid/" }) as Client;
      assert.equal(c.apiKey, "explicit-key");
      assert.equal(c.apiUrl, "http://explicit.invalid");
      assert.equal((resolveClient({}) as Client).apiUrl, "http://env.invalid");
    },
  );
  assert.deepEqual(warnings, []);
});

test("a Temporal record is sent with COMMITLAYER_RECORDER_KEY to COMMITLAYER_API_URL, whatever else the process holds", async () => {
  const info: ActivityInfoLike = { activityId: "7", activityType: "refundOrder", attempt: 1, workflowExecution: { workflowId: "wf_1", runId: "run_1" } };
  const mapping = { activities: { refundOrder: { contract_id: "refund_workflow", subject: "args.0.orderId" } } };
  const sent: { url: string; key: string | null }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    sent.push({ url: String(input), key: new Headers(init?.headers).get("x-api-key") });
    return new Response(JSON.stringify({ action: {}, verification: {} }), { status: 201, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const env = { COMMITLAYER_RECORDER_KEY: "rec-key", COMMITLAYER_API_KEY: "legacy-key", COMMITLAYER_EVIDENCE_KEY: "evidence-key", COMMITLAYER_API_URL: "http://env.invalid" };
    await withEnv(env, async () => {
      const inbound = commitlayerActivityInterceptors({ mapping, info: () => info })().inbound;
      assert.equal(await inbound.execute({ args: [{ orderId: "o1" }] }, async () => "done"), "done");
    });
  } finally {
    globalThis.fetch = real;
  }
  assert.deepEqual(sent, [{ url: "http://env.invalid/v1/actions", key: "rec-key" }]);
});
