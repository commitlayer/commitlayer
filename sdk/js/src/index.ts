/**
 * CommitLayer JavaScript SDK: record agent actions, push facts, read verdicts, draft contracts.
 *
 * Zero dependencies; uses the global `fetch` (Node 18+ or a browser). One client per workspace API key:
 *
 *     import { Client } from "commitlayer";
 *
 *     const cl = new Client({ apiKey: "spk_..." }); // COMMITLAYER_API_KEY / COMMITLAYER_API_URL are read when omitted
 *
 *     const refund = cl.action("refund_workflow", {
 *       subject: (orderId) => orderId,
 *       params: (_orderId, amount) => ({ amount }),
 *     })(async (orderId, amount) => { ... }); // the agent's own work, unchanged
 *
 * The wrapper records the action BEFORE the function runs, so the request time precedes every effect, and never
 * changes what the function returns. Recording failures are logged and swallowed by default (`failOpen: true`):
 * a verifier that is unreachable must not stop the business action, and the gap shows up as an action that was
 * never recorded rather than one that never happened. Set `failOpen: false` to throw.
 *
 * Verification itself is done by the API from the systems of record; nothing here consults the function's result.
 */

export const VERSION = "0.1.0";

export type Json = Record<string, unknown>;

/** Low-level transport, injectable for tests: resolves with the HTTP status and the parsed JSON body. */
export type Transport = (
  method: string,
  path: string,
  body?: Json | null,
) => Promise<{ status: number; json: unknown }>;

export interface ClientOptions {
  apiKey?: string;
  apiUrl?: string;
  timeoutMs?: number;
  transport?: Transport;
  agentId?: string;
}

export interface RecordActionOptions {
  params?: Json;
  actionId?: string;
  actionType?: string;
  subjectKind?: string;
  workflowId?: string;
  agentId?: string;
  requestedAt?: string;
  channel?: string;
  /** The action id of an earlier action this one corrects. Sent only when given. */
  compensates?: string;
  /** Pin the contract version the action is verified under; the active version when omitted. */
  contractVersion?: number;
}

export interface PushFactsOptions {
  links?: Json[];
  /** Source to time: everything up to that time has been sent for the source; settlement waits on it. */
  watermarks?: Record<string, string>;
}

export interface VerifyOptions {
  actionId?: string;
  action?: Json;
  facts?: Json[];
  links?: Json[];
  dryRun?: boolean;
}

export interface DraftContractOptions {
  contractId?: string;
  lookbackDays?: number;
  lookahead?: string;
  minShare?: number;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyFn = (...args: any[]) => any;

export interface ActionOptions<F extends AnyFn> {
  /** A function over the wrapped function's arguments returning the subject id, or a 0-based argument index. */
  subject: ((...args: Parameters<F>) => unknown) | number;
  /** The parameters the contract limits (an amount, a tier), computed from the arguments. */
  params?: (...args: Parameters<F>) => Json;
  actionType?: string;
  subjectKind?: string;
  workflowId?: string | ((...args: Parameters<F>) => string | undefined);
  agentId?: string;
  /** Makes retries idempotent: the same id updates the one claim instead of recording two. */
  actionId?: (...args: Parameters<F>) => string;
  compensates?: string | ((...args: Parameters<F>) => string | undefined);
  failOpen?: boolean;
  onRecorded?: (recorded: Json) => void;
}

/** The wrapped function: same arguments, always async, with the last API response attached. */
export type Wrapped<F extends AnyFn> = ((...args: Parameters<F>) => Promise<Awaited<ReturnType<F>>>) & {
  lastRecorded: Json | null;
};

export class CommitLayerError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "CommitLayerError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  override toString(): string {
    return `${this.code} (${this.status}): ${this.message}`;
  }
}

function env(name: string): string | undefined {
  // `process` is absent in browsers; the option or the default applies there.
  const p = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return p?.env?.[name];
}

function now(): string {
  // The API takes whole seconds, as the Python SDK sends them.
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function newActionId(): string {
  return "act_" + crypto.randomUUID().replace(/-/g, "");
}

export class Client {
  readonly apiKey: string;
  readonly apiUrl: string;
  readonly timeoutMs: number;
  readonly agentId: string | undefined;
  private readonly transport: Transport;

  constructor(options: ClientOptions = {}) {
    this.apiKey = options.apiKey || env("COMMITLAYER_API_KEY") || "";
    this.apiUrl = (options.apiUrl || env("COMMITLAYER_API_URL") || "https://api.commitlayer.ai").replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? 20_000;
    this.agentId = options.agentId;
    this.transport = options.transport ?? ((m, p, b) => this.http(m, p, b));
    if (!this.apiKey && !options.transport) {
      throw new Error("apiKey is required (or set COMMITLAYER_API_KEY)");
    }
  }

  // ---------------- transport ----------------

  private async http(method: string, path: string, body?: Json | null): Promise<{ status: number; json: unknown }> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.apiUrl + path, {
        method,
        headers: {
          "X-API-Key": this.apiKey,
          "Content-Type": "application/json",
          Accept: "application/json",
          // Browsers refuse to set User-Agent and drop it silently; Node sends it.
          "User-Agent": `commitlayer-js/${VERSION}`,
        },
        body: body == null ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const raw = await res.text();
      if (!raw) return { status: res.status, json: {} };
      try {
        return { status: res.status, json: JSON.parse(raw) };
      } catch {
        const message = raw.slice(0, 300);
        if (res.status >= 400) return { status: res.status, json: { error: { code: "http_error", message } } };
        throw new CommitLayerError(res.status, "invalid_response", message);
      }
    } finally {
      clearTimeout(timer);
    }
  }

  private async call(method: string, path: string, body?: Json | null): Promise<Json> {
    const { status, json } = await this.transport(method, path, body);
    if (status >= 400) {
      const err = (json as { error?: unknown } | null)?.error;
      if (err && typeof err === "object") {
        const e = err as { code?: string; message?: string; details?: unknown };
        throw new CommitLayerError(status, e.code ?? "error", e.message ?? "", e.details);
      }
      throw new CommitLayerError(status, "error", JSON.stringify(json).slice(0, 300));
    }
    return json as Json;
  }

  // ---------------- actions and facts ----------------

  /**
   * `POST /v1/actions`: record one action. Returns `{action, verification, allowance}`. The verification is
   * provisional until its settlement window closes and every source it reads has caught up.
   */
  async recordAction(contractId: string, subjectId: string, opts: RecordActionOptions = {}): Promise<Json> {
    const body: Json = {
      action_id: opts.actionId || newActionId(),
      contract_id: contractId,
      subject_id: String(subjectId),
      params: opts.params ?? {},
      requested_at: opts.requestedAt || now(),
    };
    const optional: [string, string | number | undefined][] = [
      ["action_type", opts.actionType],
      ["subject_kind", opts.subjectKind],
      ["workflow_id", opts.workflowId],
      ["agent_id", opts.agentId ?? this.agentId],
      ["channel", opts.channel],
      ["compensates", opts.compensates],
      ["contract_version", opts.contractVersion],
    ];
    for (const [k, v] of optional) if (v != null) body[k] = v;
    return this.call("POST", "/v1/actions", body);
  }

  /**
   * `POST /v1/facts`. `watermarks` (source to time) states that everything up to that time has been sent for the
   * source; settlement waits on it instead of the clock.
   */
  async pushFacts(facts: Json[], opts: PushFactsOptions = {}): Promise<Json> {
    const body: Json = { facts };
    if (opts.links?.length) body.links = opts.links;
    if (opts.watermarks && Object.keys(opts.watermarks).length) body.watermarks = opts.watermarks;
    return this.call("POST", "/v1/facts", body);
  }

  /** `POST /v1/actions/verify`: verify now, by id or inline. `dryRun` stores nothing. */
  async verify(opts: VerifyOptions = {}): Promise<Json> {
    const body: Json = { dry_run: opts.dryRun ?? false };
    if (opts.actionId) body.action_id = opts.actionId;
    if (opts.action) body.action = opts.action;
    if (opts.facts?.length) body.facts = opts.facts;
    if (opts.links?.length) body.links = opts.links;
    return this.call("POST", "/v1/actions/verify", body);
  }

  /** Any endpoint, with the client's auth and error handling: `request("GET", "/v1/actions/x/wait?timeout=30")`. */
  async request(method: string, path: string, body?: Json | null): Promise<Json> {
    return this.call(method, path, body);
  }

  /** `GET /v1/actions/{ref}`: `ref` is the `action_id` or the internal id. */
  async getAction(ref: string): Promise<Json> {
    return this.call("GET", `/v1/actions/${ref}`);
  }

  /**
   * The completion gate: resolves when the action's verdict settles or `timeoutMs` passes, with the
   * verification (`status` is `settled` or, on timeout, `pending`; `timed_out` says which). The API holds each
   * call for up to 60 seconds; this loops. A workflow should not tell the customer "done" before this returns a
   * settled PASS; FAIL routes to recovery; a timeout is the workflow's decision.
   */
  async waitForSettlement(ref: string, opts: { timeoutMs?: number } = {}): Promise<Json> {
    const deadline = Date.now() + Math.max(0, opts.timeoutMs ?? 300_000);
    for (;;) {
      const budget = Math.max(0, Math.min(60_000, deadline - Date.now()));
      const v = await this.call("GET", `/v1/actions/${ref}/wait?timeout=${Math.round(budget / 1000)}`);
      if (!v.timed_out || budget < 1000 || Date.now() >= deadline) return v;
    }
  }

  // ---------------- contracts ----------------

  /**
   * `POST /v1/contracts/draft`: a contract drafted from recorded actions and the facts around them.
   * Returns `{yaml, contract, errors, warnings, stats}`; nothing is saved.
   */
  async draftContract(actionType?: string, opts: DraftContractOptions = {}): Promise<Json> {
    return this.call("POST", "/v1/contracts/draft", {
      action_type: actionType ?? null,
      contract_id: opts.contractId ?? null,
      lookback_days: opts.lookbackDays ?? 30,
      lookahead: opts.lookahead ?? "24h",
      min_share: opts.minShare ?? 0.8,
    });
  }

  /** `POST /v1/contracts/validate`: errors, compiled lenses and weakening warnings against the version it would replace. */
  async validateContract(yaml: string): Promise<Json> {
    return this.call("POST", "/v1/contracts/validate", { yaml });
  }

  /** `POST /v1/contracts`: save a version. The response carries `warnings` when it is weaker than the last one. */
  async saveContract(yaml: string): Promise<Json> {
    return this.call("POST", "/v1/contracts", { yaml });
  }

  // ---------------- wrapper ----------------

  /**
   * Record each call of the wrapped function as an action under `contractId` before it runs.
   *
   * `subject` is a function over the call's arguments returning the subject id, or the 0-based index of the
   * argument holding it. `params` is a function over the arguments returning the parameters the contract limits.
   * `actionId` makes retries idempotent: give it the same id and the API updates the one claim rather than
   * recording two. The API response is passed to `onRecorded` and stored on the function as `lastRecorded`.
   */
  action<F extends AnyFn>(contractId: string, opts: ActionOptions<F>): (fn: F) => Wrapped<F> {
    const { subject, params, actionType, subjectKind, workflowId, agentId, actionId, compensates, onRecorded } = opts;
    const failOpen = opts.failOpen ?? true;
    const pick = <T>(v: T | ((...args: Parameters<F>) => T), args: Parameters<F>): T =>
      typeof v === "function" ? (v as (...a: Parameters<F>) => T)(...args) : v;

    return (fn: F): Wrapped<F> => {
      const client = this;
      const wrapped = async function (this: unknown, ...args: Parameters<F>) {
        try {
          const subj = typeof subject === "number" ? args[subject] : subject(...args);
          if (subj == null) throw new Error("subject id is missing from the call's arguments");
          const rec = await client.recordAction(contractId, String(subj), {
            params: params ? params(...args) : {},
            actionId: actionId ? actionId(...args) : undefined,
            actionType,
            subjectKind,
            workflowId: pick(workflowId, args),
            agentId,
            compensates: pick(compensates, args),
          });
          wrapped.lastRecorded = rec;
          onRecorded?.(rec);
        } catch (e) {
          // The business action must not depend on the verifier.
          wrapped.lastRecorded = null;
          if (!failOpen) throw e;
          console.warn(`commitlayer: action not recorded for ${fn.name || "<anonymous>"}: ${String(e)}`);
        }
        return fn.apply(this, args) as Awaited<ReturnType<F>>;
      } as Wrapped<F>;
      wrapped.lastRecorded = null;
      return wrapped;
    };
  }
}
