import { test } from "node:test";
import assert from "node:assert/strict";

import { Client, CommitLayerError, type Json } from "../src/index.js";
import { recordedStep, waitForVerdict, type StepLike } from "../src/inngest.js";
import { commitlayerActivityInterceptors, createCommitLayerActivities, type ActivityInfoLike } from "../src/temporal.js";
import { fakeLog, settledVerification } from "./helpers.js";

// What each integration does when the API refuses a call with 429 or 503 and Retry-After, through the client
// the integration builds itself and a fake `fetch`. The SDK retries from 0.1.4 on; with an earlier SDK installed
// there is nothing to observe and these tests are skipped.
const sdkRetries = "maxRetries" in new Client({ apiKey: "spk_test" });
const NEEDS_SDK = { skip: sdkRetries ? false : "needs commitlayer 0.1.4 or later, the first SDK that retries" };

interface Reply {
  status: number;
  retryAfter?: string;
  json?: Json;
}

/** Replace the global `fetch` for one test: each call gets the next reply (the last one repeats) and is kept. */
async function withFetch(replies: Reply[], run: (sent: { method: string; url: string; body: string | null }[]) => Promise<void>): Promise<void> {
  const sent: { method: string; url: string; body: string | null }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    sent.push({ method: init?.method ?? "GET", url: String(input), body: typeof init?.body === "string" ? init.body : null });
    const reply = replies[Math.min(sent.length - 1, replies.length - 1)];
    const headers = new Headers({ "content-type": "application/json" });
    if (reply.retryAfter !== undefined) headers.set("Retry-After", reply.retryAfter);
    const json = reply.json ?? (reply.status < 400 ? { action: {}, verification: {} } : { error: { code: "workspace_busy", message: "not applied" } });
    return new Response(JSON.stringify(json), { status: reply.status, headers });
  }) as typeof fetch;
  try {
    await run(sent);
  } finally {
    globalThis.fetch = real;
  }
}

const api = { apiKey: "spk_test", apiUrl: "http://api.invalid" };
const refused: Reply = { status: 429, retryAfter: "0" };
const ok: Reply = { status: 201 };

const info: ActivityInfoLike = {
  activityId: "7",
  activityType: "refundOrder",
  attempt: 1,
  workflowExecution: { workflowId: "wf_1", runId: "run_1" },
};
const mapping = { activities: { refundOrder: { contract_id: "refund_workflow", subject: "args.0.orderId" } } };

function step(): StepLike {
  return { run: async (_id, fn) => fn() as unknown, sleep: async () => {} };
}

test("temporal interceptor: a refused record is sent again, twice at most, with the same body", NEEDS_SDK, async () => {
  await withFetch([refused, ok], async (sent) => {
    let ran = 0;
    const inbound = commitlayerActivityInterceptors({ ...api, mapping, info: () => info })().inbound;
    assert.equal(await inbound.execute({ args: [{ orderId: "o1" }] }, async () => ((ran += 1), "done")), "done");
    assert.equal(sent.length, 2);
    assert.equal(sent[1].body, sent[0].body);
    assert.equal((JSON.parse(sent[0].body!) as Json).action_id, "temporal:wf_1:7");
    assert.equal(ran, 1);
  });

  await withFetch([refused], async (sent) => {
    const { lines, log } = fakeLog();
    let ran = 0;
    const inbound = commitlayerActivityInterceptors({ ...api, mapping, info: () => info, log })().inbound;
    await inbound.execute({ args: [{ orderId: "o1" }] }, async () => (ran += 1));
    assert.equal(sent.length, 3); // the first attempt and two retries
    assert.equal(ran, 1); // fail-open: the activity still runs
    assert.equal(lines.length, 1);
    assert.match(lines[0], /recording failed for temporal:wf_1:7 \(refundOrder\): workspace_busy \(429\): not applied: activity runs anyway/);
  });
});

test("temporal interceptor: a wait above two seconds is not made, and the caller's options win", NEEDS_SDK, async () => {
  await withFetch([{ status: 503, retryAfter: "3" }, ok], async (sent) => {
    const { log } = fakeLog();
    const inbound = commitlayerActivityInterceptors({ ...api, mapping, info: () => info, log })().inbound;
    await inbound.execute({ args: [{ orderId: "o1" }] }, async () => "done");
    assert.equal(sent.length, 1);
  });
  await withFetch([refused, ok], async (sent) => {
    const { log } = fakeLog();
    const inbound = commitlayerActivityInterceptors({ ...api, mapping, info: () => info, log, maxRetries: 0 })().inbound;
    await inbound.execute({ args: [{ orderId: "o1" }] }, async () => "done");
    assert.equal(sent.length, 1);
  });
});

test("temporal commitlayerVerify: a refused poll is not retried by the SDK; the activity fails and Temporal retries it", NEEDS_SDK, async () => {
  await withFetch([refused, { status: 200, json: settledVerification("a1") }], async (sent) => {
    const beats: unknown[] = [];
    const { commitlayerVerify } = createCommitLayerActivities({
      ...api,
      context: () => ({ info, heartbeat: (d) => void beats.push(d), sleep: async () => {} }),
    });
    await assert.rejects(commitlayerVerify("a1", { holdMs: 0 }), (e: unknown) => e instanceof CommitLayerError && e.status === 429);
    assert.equal(sent.length, 1);
    assert.deepEqual(beats, []);
  });
});

test("inngest recordedStep: a refused record is sent again inside the record step, twice at most", NEEDS_SDK, async () => {
  const options = { ...api, runId: "run_1", contract_id: "refund_workflow", subject_id: "o1" };
  await withFetch([refused, ok], async (sent) => {
    assert.equal(await recordedStep(step(), "refund", async () => "refunded", options), "refunded");
    assert.equal(sent.length, 2);
    assert.equal(sent[1].body, sent[0].body);
    assert.equal((JSON.parse(sent[0].body!) as Json).action_id, "inngest:run_1:refund");
  });
  await withFetch([refused], async (sent) => {
    const { log } = fakeLog();
    await assert.rejects(
      recordedStep(step(), "refund", async () => "refunded", { ...options, failOpen: false, log }),
      (e: unknown) => e instanceof CommitLayerError && e.code === "workspace_busy",
    );
    assert.equal(sent.length, 3);
  });
});

test("inngest waitForVerdict: a refused poll is not retried by the SDK; the step throws and Inngest retries it", NEEDS_SDK, async () => {
  await withFetch([refused, { status: 200, json: settledVerification("a1") }], async (sent) => {
    await assert.rejects(waitForVerdict(step(), "a1", { ...api, holdMs: 0 }), (e: unknown) => e instanceof CommitLayerError && e.status === 429);
    assert.equal(sent.length, 1);
  });
});
