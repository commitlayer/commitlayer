import { Client, type Json, type Transport } from "commitlayer";

export interface Call {
  method: string;
  path: string;
  body: Json | null | undefined;
}

export interface FakeApiOptions {
  /** Answers to successive GETs; the last one repeats. */
  gets?: Json[];
  /** HTTP status for POST /v1/actions; default 201. */
  postStatus?: number;
  /** HTTP status for GETs; default 200. */
  getStatus?: number;
}

/** A CommitLayer client over a fake transport that records every call. */
export function fakeApi(opts: FakeApiOptions = {}) {
  const calls: Call[] = [];
  let gets = 0;
  const transport: Transport = async (method, path, body) => {
    calls.push({ method, path, body });
    if (method === "POST") {
      const status = opts.postStatus ?? 201;
      if (status >= 400) return { status, json: { error: { code: "internal_error", message: "store unavailable" } } };
      return { status, json: { action: { action_id: body?.action_id, latest: null }, verification: {}, allowance: {} } };
    }
    const status = opts.getStatus ?? 200;
    if (status >= 400) return { status, json: { error: { code: "not_found", message: "no such action" } } };
    const list = opts.gets ?? [];
    const json = list[Math.min(gets, list.length - 1)] ?? {};
    gets += 1;
    return { status, json };
  };
  const client = new Client({ transport });
  return {
    client,
    calls,
    posts: () => calls.filter((c) => c.method === "POST"),
    getsMade: () => calls.filter((c) => c.method === "GET"),
  };
}

export function pendingVerification(actionId: string, observed = 1): Json {
  return {
    action: { action_id: actionId, contract_id: "refund_workflow", latest: null },
    status: "pending",
    settles_at: "2026-10-15T10:57:00Z",
    lenses: {
      outcome: { verdict: "UNKNOWN", reason_codes: ["no_structured_signal"], reason_labels: ["No structured signal"] },
      policy: { verdict: "UNKNOWN", reason_codes: ["no_structured_signal"], reason_labels: ["No structured signal"] },
    },
    summary: { expected: 4, observed, headline: `PENDING — ${observed} of 4 expected effects observed so far; window closes 2026-10-15T10:57:00Z` },
  };
}

export function settledVerification(actionId: string, outcome = "PASS", policy = outcome): Json {
  return {
    action: { action_id: actionId, contract_id: "refund_workflow" },
    status: "settled",
    settles_at: "2026-10-15T10:57:00Z",
    lenses: {
      outcome: { verdict: outcome, reason_codes: ["effect_observed:stripe.refund_created"], reason_labels: ["Expected effect observed: stripe refund_created"] },
      policy: { verdict: policy, reason_codes: policy === "PASS" ? ["effect_observed:stripe.refund_created"] : ["limit_exceeded:amount"], reason_labels: [] },
    },
    summary: { expected: 4, observed: 4, headline: `${policy} — headline` },
  };
}

/** A log sink that keeps every line. */
export function fakeLog() {
  const lines: string[] = [];
  return { lines, log: (m: string) => void lines.push(m) };
}
