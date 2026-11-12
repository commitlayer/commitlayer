/**
 * CommitLayer for x402: observe the payment protocol on `fetch` and record what was bought.
 *
 * x402 v2 is three headers on an HTTP exchange: `PAYMENT-REQUIRED` on a 402 (the quote: `accepts[]` with
 * `scheme`, `network`, `amount` in atomic units, `asset`, `payTo`), `PAYMENT-SIGNATURE` on the paid retry (the
 * accepted quote and, for the exact scheme, `payload.authorization.nonce`), and `PAYMENT-RESPONSE` on the final
 * response (`success`, `transaction`, `network`, `payer`, `errorReason`).
 *
 * The observer does not pay, sign or hold a key. It wraps the `fetch` the x402 client library uses, records the
 * purchase as a CommitLayer action (`purchase_<kind>` on the subject `<nonce>`) when a request carries
 * `PAYMENT-SIGNATURE`, and pushes a `settlement` fact (settled, pending or failed) and a `delivered` or
 * `delivery_failed` fact from the final response. Facts travel with the `evidence` client when one is given (a
 * gateway holding a `facts:write` key), else with the recorder client, in which case the service classes them
 * `agent_self_report`: the agent's own observation, shown but never counted as proof.
 *
 * Nothing sensitive is kept: no body, no signature value, no credential. Recording is fail-open.
 */

import type { Json } from "commitlayer";

export const X402_VERSION = "0.1.3";

export const KINDS = ["api_call", "browser_session", "dataset", "model_inference", "compute_resource", "mcp_tool_call"] as const;
export type Kind = (typeof KINDS)[number];

const H_REQUIRED = ["payment-required", "x-payment-required"];
const H_SIGNATURE = ["payment-signature", "x-payment"];
const H_RESPONSE = ["payment-response", "x-payment-response"];

/** What the observer needs from a CommitLayer client; `Client` from `commitlayer` satisfies it. */
export interface X402Client {
  recordAction(contractId: string, subjectId: string, opts?: Record<string, unknown>): Promise<Json>;
  pushFacts(facts: Json[], opts?: Record<string, unknown>): Promise<Json>;
}

export interface X402ObserverOptions {
  recorder: X402Client;
  /** A client with a `facts:write` key, for the gateway role. Without it facts go through the recorder. */
  evidence?: X402Client;
  kind?: Kind;
  contractId?: string;
  contractVersion?: number;
  /** The asset's decimals (USDC: 6). `null` keeps atomic units. */
  decimals?: number | null;
  agentId?: string;
  workflowId?: string;
  extraParams?: Json;
  failOpen?: boolean;
  log?: (message: string) => void;
}

export interface Quote {
  url: string;
  scheme?: string;
  network?: string;
  amountAtomic?: string;
  asset?: string;
  payTo?: string;
}

export interface Purchase {
  actionId: string;
  subjectId: string;
  url: string;
  quote?: Quote;
  recorded?: Json;
  settlement?: "settled" | "pending" | "failed";
  delivered?: boolean;
}

/** A base64 JSON header as an object; undefined when absent or not decodable. */
export function decodeHeader(value: string | null | undefined): Json | undefined {
  if (!value) return undefined;
  try {
    const raw = Buffer.from(value, "base64").toString("utf8");
    const out: unknown = JSON.parse(raw);
    return out !== null && typeof out === "object" && !Array.isArray(out) ? (out as Json) : undefined;
  } catch {
    return undefined;
  }
}

/** `accepts[].amount` (atomic units) in the asset's major units. */
export function amountFromAtomic(atomic: unknown, decimals: number | null | undefined): number | undefined {
  const s = String(atomic);
  if (!/^\d+$/.test(s)) return undefined;
  if (decimals === null || decimals === undefined) return Number(s);
  return Number(BigInt(s)) / 10 ** decimals;
}

export function settlementStatus(header: Json): "settled" | "pending" | "failed" {
  if (header.success === true) return "settled";
  const reason = String(header.errorReason ?? header.error_reason ?? "");
  return reason === "settlement_pending" ? "pending" : "failed";
}

function header(h: Headers | HeadersInit | undefined, names: string[]): string | undefined {
  if (!h) return undefined;
  const hs = h instanceof Headers ? h : new Headers(h);
  for (const n of names) {
    const v = hs.get(n);
    if (v) return v;
  }
  return undefined;
}

function now(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function host(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

export class X402Observer {
  readonly quotes = new Map<string, Quote>();
  readonly purchases: Purchase[] = [];
  private readonly o: Required<Pick<X402ObserverOptions, "kind" | "contractId" | "failOpen">> & X402ObserverOptions;

  constructor(opts: X402ObserverOptions) {
    const kind = opts.kind ?? "api_call";
    if (!KINDS.includes(kind)) throw new Error(`kind must be one of ${KINDS.join(", ")}`);
    this.o = { ...opts, kind, contractId: opts.contractId ?? `x402_purchase_${kind}`, failOpen: opts.failOpen ?? true };
  }

  get last(): Purchase | undefined {
    return this.purchases[this.purchases.length - 1];
  }

  /** A `fetch` that watches the x402 headers of every call. Pass the x402 client library's fetch (the one that pays). */
  wrap(fetchImpl: typeof fetch = fetch): typeof fetch {
    const self = this;
    return (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const reqHeaders = init?.headers ?? (input instanceof Request ? input.headers : undefined);
      const purchase = await self.onRequest(url, reqHeaders);
      const res = await fetchImpl(input, init);
      await self.onResponse(url, res, purchase);
      return res;
    }) as typeof fetch;
  }

  /** A request of the client: one carrying `PAYMENT-SIGNATURE` is a purchase, recorded now. */
  async onRequest(url: string, headers: Headers | HeadersInit | undefined): Promise<Purchase | undefined> {
    const sigValue = header(headers, H_SIGNATURE);
    const sig = decodeHeader(sigValue);
    if (!sig) return undefined;
    const accepted = sig.accepted && typeof sig.accepted === "object" && !Array.isArray(sig.accepted) ? (sig.accepted as Json) : undefined;
    const quote = accepted ? this.quoteFrom(url, accepted) : this.quotes.get(url);
    const nonce = this.nonce(sig);
    const subjectId = nonce ?? `sig:${await sha256(sigValue ?? "")}`.slice(0, 44);
    const params: Json = { resource: url, provider: host(url), ...(this.o.extraParams ?? {}) };
    if (quote) {
      Object.assign(params, this.amounts(quote));
      if (quote.asset) params.asset = quote.asset;
      if (quote.network) params.network = quote.network;
      if (quote.payTo) params.pay_to = quote.payTo;
      if (quote.scheme) params.scheme = quote.scheme;
    }
    const payer = this.payer(sig);
    if (payer) params.payer = payer;
    const p: Purchase = { actionId: `purchase:${subjectId.slice(0, 66)}`, subjectId, url, quote };
    try {
      const opts: Record<string, unknown> = {
        actionId: p.actionId,
        actionType: `purchase_${this.o.kind}`,
        subjectKind: "payment",
        params,
        channel: "x402",
      };
      if (this.o.contractVersion !== undefined) opts.contractVersion = this.o.contractVersion;
      if (this.o.agentId) opts.agentId = this.o.agentId;
      if (this.o.workflowId) opts.workflowId = this.o.workflowId;
      p.recorded = await this.o.recorder.recordAction(this.o.contractId, subjectId, opts);
    } catch (e) {
      if (!this.o.failOpen) throw e;
      this.o.log?.(`commitlayer x402: purchase ${p.actionId} not recorded: ${String(e)}`);
    }
    this.purchases.push(p);
    return p;
  }

  /** A response of the client: a 402 is a quote; the response to a paid request closes the purchase. */
  async onResponse(url: string, res: Response, purchase?: Purchase): Promise<void> {
    if (res.status === 402) {
      const required = decodeHeader(header(res.headers, H_REQUIRED));
      if (required) this.rememberQuote(url, required);
    }
    if (!purchase) return;
    const facts: Json[] = [];
    const at = now();
    const settlement = decodeHeader(header(res.headers, H_RESPONSE));
    if (settlement) {
      const st = settlementStatus(settlement);
      purchase.settlement = st;
      const attrs: Json = { status: st };
      if (settlement.transaction) attrs.transaction = settlement.transaction;
      attrs.network = settlement.network ?? purchase.quote?.network ?? null;
      if (settlement.payer) attrs.payer = settlement.payer;
      if (purchase.quote) {
        Object.assign(attrs, this.amounts(purchase.quote));
        if (purchase.quote.asset) attrs.asset = purchase.quote.asset;
        if (purchase.quote.payTo) attrs.pay_to = purchase.quote.payTo;
      }
      if (st !== "settled") attrs.error_reason = settlement.errorReason ?? settlement.error_reason ?? null;
      for (const k of Object.keys(attrs)) if (attrs[k] === null || attrs[k] === undefined) delete attrs[k];
      facts.push({
        source: "x402",
        subject_id: purchase.subjectId,
        subject_kind: "payment",
        fact_type: "settlement",
        occurred_at: at,
        attributes: attrs,
        raw_ref: `x402:settlement:${purchase.subjectId}:${st}`,
      });
    }
    const delivered = res.status >= 200 && res.status < 300;
    purchase.delivered = delivered;
    const dattrs: Json = { kind: this.o.kind, http_status: res.status, resource: url };
    const length = res.headers.get("content-length");
    if (length && /^\d+$/.test(length)) dattrs.bytes = Number(length);
    facts.push({
      source: "delivery",
      subject_id: purchase.subjectId,
      subject_kind: "payment",
      fact_type: delivered ? "delivered" : "delivery_failed",
      occurred_at: at,
      attributes: dattrs,
      raw_ref: `x402:delivery:${purchase.subjectId}`,
    });
    try {
      await (this.o.evidence ?? this.o.recorder).pushFacts(facts);
    } catch (e) {
      if (!this.o.failOpen) throw e;
      this.o.log?.(`commitlayer x402: ${facts.length} fact(s) not pushed: ${String(e)}`);
    }
  }

  private rememberQuote(url: string, required: Json): void {
    const accepts = required.accepts;
    const first = Array.isArray(accepts) && accepts[0] && typeof accepts[0] === "object" ? (accepts[0] as Json) : undefined;
    if (first) this.quotes.set(url, this.quoteFrom(url, first));
  }

  private quoteFrom(url: string, req: Json): Quote {
    const amount = req.amount ?? req.maxAmountRequired; // v1 spelling, best effort
    const q: Quote = { url };
    if (typeof req.scheme === "string") q.scheme = req.scheme;
    if (typeof req.network === "string") q.network = req.network;
    if (amount !== undefined && amount !== null) q.amountAtomic = String(amount);
    if (typeof req.asset === "string") q.asset = req.asset;
    if (typeof req.payTo === "string") q.payTo = req.payTo;
    return q;
  }

  private amounts(q: Quote): Json {
    const out: Json = {};
    if (q.amountAtomic !== undefined) {
      out.amount_atomic = q.amountAtomic;
      const major = amountFromAtomic(q.amountAtomic, this.o.decimals === undefined ? 6 : this.o.decimals);
      if (major !== undefined) out.amount = major;
    }
    return out;
  }

  private nonce(sig: Json): string | undefined {
    const payload = sig.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
    const auth = (payload as Json).authorization;
    if (auth && typeof auth === "object" && !Array.isArray(auth) && (auth as Json).nonce) return String((auth as Json).nonce);
    return undefined;
  }

  private payer(sig: Json): string | undefined {
    const payload = sig.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
    const auth = (payload as Json).authorization;
    if (auth && typeof auth === "object" && !Array.isArray(auth) && (auth as Json).from) return String((auth as Json).from);
    return undefined;
  }
}

/** The observer, from options. */
export function x402Observer(opts: X402ObserverOptions): X402Observer {
  return new X402Observer(opts);
}

async function sha256(s: string): Promise<string> {
  const data = new TextEncoder().encode(s);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
