import { test } from "node:test";
import assert from "node:assert/strict";

import type { Json } from "commitlayer";
import { X402Observer, amountFromAtomic, decodeHeader, settlementStatus } from "../src/x402.js";
import { fakeApi } from "./helpers.js";

const NONCE = "0x6e0f6b2d4c7a4a7e9d1b3f5c8a2e4b6d7f9a1c3e5b7d9f1a3c5e7b9d1f3a5c7e";
const URL_ = "https://api.example-data.com/v1/quote";
const ACCEPT = {
  scheme: "exact",
  network: "eip155:8453",
  amount: "50000",
  asset: "0x833589fCB6eE2CBd66d4A3DBe0B0F7F9b9B0b4C1",
  payTo: "0x2222222222222222222222222222222222222222",
  maxTimeoutSeconds: 60,
};

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");
const required = () => b64({ x402Version: 2, resource: { url: URL_ }, accepts: [ACCEPT] });
const signature = () =>
  b64({
    x402Version: 2,
    accepted: ACCEPT,
    payload: {
      signature: "0xdeadbeef",
      authorization: { from: "0x1111111111111111111111111111111111111111", to: ACCEPT.payTo, value: "50000", validAfter: "0", validBefore: "9999999999", nonce: NONCE },
    },
  });
const paymentResponse = (success: boolean, reason?: string) =>
  b64({ success, transaction: success || reason ? "0x3f2a" : "", network: "eip155:8453", payer: "0x1111111111111111111111111111111111111111", ...(reason ? { errorReason: reason } : {}) });

/** The paid resource: 402 without a signature, the resource with one. */
function server(finalStatus = 200, settle: boolean | null = true, reason?: string): typeof fetch {
  return (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const h = new Headers(init?.headers);
    if (!h.get("payment-signature")) return new Response("{}", { status: 402, headers: { "PAYMENT-REQUIRED": required() } });
    const headers: Record<string, string> = { "content-length": "1200" };
    if (settle !== null) headers["PAYMENT-RESPONSE"] = paymentResponse(settle, reason);
    return new Response("x".repeat(1200), { status: finalStatus, headers });
  }) as typeof fetch;
}

async function buy(paidFetch: typeof fetch): Promise<Response> {
  const first = await paidFetch(URL_);
  assert.equal(first.status, 402);
  return paidFetch(URL_, { headers: { "PAYMENT-SIGNATURE": signature() } });
}

test("x402: quote, payment and settlement become one action and two facts, facts through the evidence key", async () => {
  const recorder = fakeApi();
  const evidence = fakeApi();
  const obs = new X402Observer({ recorder: recorder.client, evidence: evidence.client, kind: "api_call", contractVersion: 3, agentId: "bot" });
  const res = await buy(obs.wrap(server()));
  assert.equal(res.status, 200);
  const [post] = recorder.posts();
  const body = post.body as Json;
  assert.equal(body.contract_id, "x402_purchase_api_call");
  assert.equal(body.contract_version, 3);
  assert.equal(body.subject_id, NONCE);
  assert.equal(body.subject_kind, "payment");
  assert.equal(body.action_id, `purchase:${NONCE}`);
  const params = body.params as Json;
  assert.equal(params.amount, 0.05);
  assert.equal(params.amount_atomic, "50000");
  assert.equal(params.network, "eip155:8453");
  assert.equal(params.pay_to, ACCEPT.payTo);
  assert.equal(params.provider, "api.example-data.com");
  assert.equal(params.payer, "0x1111111111111111111111111111111111111111");
  assert.ok(!JSON.stringify(body).includes("deadbeef"));
  assert.equal(recorder.calls.filter((c) => c.path === "/v1/facts").length, 0);
  const [factsCall] = evidence.calls.filter((c) => c.path === "/v1/facts");
  const facts = (factsCall.body as Json).facts as Json[];
  const settlement = facts.find((f) => f.fact_type === "settlement")!;
  assert.equal(settlement.source, "x402");
  assert.equal((settlement.attributes as Json).status, "settled");
  assert.equal((settlement.attributes as Json).amount, 0.05);
  assert.equal(settlement.raw_ref, `x402:settlement:${NONCE}:settled`);
  const delivered = facts.find((f) => f.fact_type === "delivered")!;
  assert.deepEqual(delivered.attributes, { kind: "api_call", http_status: 200, resource: URL_, bytes: 1200 });
  assert.equal(obs.last?.settlement, "settled");
  assert.equal(obs.last?.delivered, true);
});

test("x402: pending and failed settlements, an error after payment, facts through the recorder when no evidence client", async () => {
  const api = fakeApi();
  const obs = new X402Observer({ recorder: api.client, kind: "dataset" });
  await buy(obs.wrap(server(200, false, "settlement_pending")));
  let facts = (api.calls.filter((c) => c.path === "/v1/facts").pop()!.body as Json).facts as Json[];
  const pending = facts.find((f) => f.fact_type === "settlement")!;
  assert.equal((pending.attributes as Json).status, "pending");
  assert.equal((pending.attributes as Json).error_reason, "settlement_pending");
  const obs2 = new X402Observer({ recorder: api.client, kind: "dataset" });
  await buy(obs2.wrap(server(500, false, "insufficient_funds")));
  facts = (api.calls.filter((c) => c.path === "/v1/facts").pop()!.body as Json).facts as Json[];
  assert.equal((facts.find((f) => f.fact_type === "settlement")!.attributes as Json).status, "failed");
  const failed = facts.find((f) => f.fact_type === "delivery_failed")!;
  assert.equal((failed.attributes as Json).http_status, 500);
  assert.equal((failed.attributes as Json).kind, "dataset");
});

test("x402: fail-open, unpaid requests, helpers", async () => {
  const down = fakeApi({ postStatus: 503 });
  const lines: string[] = [];
  const obs = new X402Observer({ recorder: down.client, kind: "mcp_tool_call", log: (m) => lines.push(m) });
  const res = await buy(obs.wrap(server()));
  assert.equal(res.status, 200);
  assert.equal(obs.last?.recorded, undefined);
  assert.ok(lines.some((l) => l.includes("not recorded")));
  const api = fakeApi();
  const obs2 = new X402Observer({ recorder: api.client });
  const free = obs2.wrap((async () => new Response("ok", { status: 200 })) as typeof fetch);
  await free("https://free.example.com/");
  assert.equal(api.calls.length, 0);
  assert.equal(obs2.purchases.length, 0);
  assert.equal(decodeHeader(undefined), undefined);
  assert.equal(decodeHeader("!!"), undefined);
  assert.equal(amountFromAtomic("50000", 6), 0.05);
  assert.equal(amountFromAtomic("7", null), 7);
  assert.equal(amountFromAtomic("x", 6), undefined);
  assert.equal(settlementStatus({ success: true }), "settled");
  assert.equal(settlementStatus({ success: false, errorReason: "settlement_pending", transaction: "0x1" }), "pending");
  assert.equal(settlementStatus({ success: false, errorReason: "insufficient_funds" }), "failed");
  assert.throws(() => new X402Observer({ recorder: api.client, kind: "pizza" as never }), /kind must be one of/);
});
