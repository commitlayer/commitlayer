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
 *
 * A request the API refuses with 429 or 503 and a `Retry-After` header is sent again after the stated wait, up to
 * `maxRetries` times (default 3; 0 turns it off). The API answers that way when it is busy or the workspace is
 * past a rate, and a retry is the same request: the same body, so the same `action_id`. The wait is the stated
 * time plus a random addition of up to twice as much, so requests refused together come back spread out. No
 * other answer is retried, and neither is a network error or a timeout.
 */

export const VERSION = "0.1.4"; // kept equal to package.json; the tests check it

export type Json = Record<string, unknown>;

/**
 * Response headers as a transport may return them: the `Headers` of a fetch response, another object with a
 * `get`, or a plain record of name to value. In a record names are matched without regard to case; a `get` is
 * asked for `retry-after` and then for `Retry-After`, so one that is case-sensitive is read under either spelling.
 */
export type ResponseHeaders =
  | { get(name: string): string | null | undefined }
  | Record<string, string | string[] | null | undefined>;

/** What a transport resolves with. `headers` is optional: without it the client never sees `Retry-After` and never retries. */
export interface TransportResponse {
  status: number;
  json: unknown;
  headers?: ResponseHeaders | null;
}

/**
 * Low-level transport, injectable for tests: resolves with the HTTP status and the parsed JSON body, and may
 * add the response headers. A retry calls it again with the same arguments.
 */
export type Transport = (method: string, path: string, body?: Json | null) => Promise<TransportResponse>;

/** One retry, as `onRetry` is told about it. It never carries the API key or the request body. */
export interface RetryInfo {
  method: string;
  path: string;
  /** The answer that is being retried: 429 or 503, and the error code of its body. */
  status: number;
  code: string;
  /** The attempt that was refused, counting from 1. */
  attempt: number;
  maxRetries: number;
  /** The wait the server stated in `Retry-After`. */
  retryAfterMs: number;
  /** The wait the client is about to make: the stated time plus the random addition, at most `maxRetryWaitMs`. */
  waitMs: number;
}

export interface ClientOptions {
  apiKey?: string;
  apiUrl?: string;
  /** Per attempt; default 20 seconds. The wait before a retry is not counted against it. */
  timeoutMs?: number;
  transport?: Transport;
  agentId?: string;
  /**
   * How many times a request refused with 429 or 503 and a `Retry-After` header is sent again; default 3,
   * 0 turns retrying off. No other answer is retried, and neither is a network error or a timeout. A negative
   * value is refused by the constructor.
   */
  maxRetries?: number;
  /**
   * The longest wait the client will make before one retry, in milliseconds; default 10000. A `Retry-After`
   * above it is not waited for: the error is thrown at once, with the stated wait in `retryAfterMs`. A value
   * above 2147483647 (the longest a timer can run, about 24 days) is taken as that.
   */
  maxRetryWaitMs?: number;
  /** Called once per retry, before the wait, e.g. to log it. What it throws is ignored. */
  onRetry?: (info: RetryInfo) => void;
  /** The wait itself; default a `setTimeout`. Replaceable (a cancellation-aware sleep, a fake in tests). */
  sleep?: (ms: number) => Promise<void>;
  /**
   * The random source of the addition to the wait, in [0, 1); default `Math.random`. The addition is this share
   * of twice the stated wait, or of what is left under `maxRetryWaitMs` when that is less. Replaceable for tests.
   */
  random?: () => number;
  /** Clock in ms, used to read a `Retry-After` given as a date; default `Date.now`. Replaceable for tests. */
  now?: () => number;
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
  /**
   * The wait the server stated in `Retry-After`, in milliseconds; undefined when the answer carried no
   * `Retry-After` the client could read.
   */
  readonly retryAfterMs: number | undefined;
  /** How many times the request was sent, this answer included; undefined when the error is not an API answer. */
  readonly attempts: number | undefined;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: unknown,
    retry: { retryAfterMs?: number; attempts?: number } = {},
  ) {
    super(message);
    this.name = "CommitLayerError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryAfterMs = retry.retryAfterMs;
    this.attempts = retry.attempts;
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

// ---------------- retry ----------------

/**
 * The random addition to a wait is at most this many times the stated wait, so a burst refused together comes
 * back spread over that time and not all at once: a wait of one stated second is between one and three. The
 * API admits a few writes of one workspace at a time and refuses the rest at once, so calls that return within
 * the same quarter second are refused together again; spread over two seconds they are admitted as they arrive.
 */
const RETRY_SPREAD = 2;

/** The longest delay a timer takes; a longer one fires at once in Node and in browsers. */
const MAX_TIMER_MS = 2_147_483_647;

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/** The `Retry-After` header of an answer, or undefined when the transport returned none. */
function retryAfterHeader(headers: ResponseHeaders | null | undefined): string | undefined {
  if (!headers || typeof headers !== "object") return undefined;
  const get = (headers as { get?: unknown }).get;
  if (typeof get === "function") {
    try {
      // A fetch `Headers` answers to any case; a `Map` or a hand-written `get` may know only one spelling.
      const v: unknown = get.call(headers, "retry-after") ?? get.call(headers, "Retry-After");
      return typeof v === "string" ? v : undefined;
    } catch {
      return undefined; // headers that cannot be read are no headers: the answer itself is still reported
    }
  }
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() !== "retry-after") continue;
    const first: unknown = Array.isArray(v) ? v[0] : v;
    if (typeof first === "string") return first;
    if (typeof first === "number") return String(first);
  }
  return undefined;
}

/**
 * `Retry-After` as milliseconds from `nowMs`: a number of seconds (whole or decimal, zero or more) or an
 * HTTP-date (`Wed, 21 Oct 2026 07:28:00 GMT`; one in the past is zero). Undefined when the header is absent or
 * is anything else, and an answer without a readable wait is never retried.
 */
function retryAfterMsOf(headers: ResponseHeaders | null | undefined, nowMs: number): number | undefined {
  const value = retryAfterHeader(headers)?.trim();
  if (!value) return undefined;
  if (/^(\d+(\.\d*)?|\.\d+)$/.test(value)) {
    // Rounded up, so the wait is never shorter than stated; toFixed drops the float noise of the product first.
    const ms = Math.ceil(Number((Number(value) * 1000).toFixed(3)));
    return Number.isFinite(ms) ? ms : undefined;
  }
  const m = /^[A-Za-z]{3}, (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(value);
  if (!m) return undefined;
  const month = MONTHS.indexOf(m[2].toLowerCase());
  if (month < 0) return undefined;
  // Not a number when the date or the clock is not one: then there is no wait to read.
  const ms = Date.UTC(Number(m[3]), month, Number(m[1]), Number(m[4]), Number(m[5]), Number(m[6])) - nowMs;
  return Number.isFinite(ms) ? Math.max(0, ms) : undefined;
}

function defaultSleep(ms: number): Promise<void> {
  // The call is waiting on this timer, so it is left referenced; once it fires nothing is left behind.
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** A count or a duration from an option: a whole number, zero or more; the default when it is not given. */
function atLeastZero(name: string, value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error(`${name} must be zero or more`);
  return Math.floor(value);
}

/**
 * The route of a request path as the server will read it: without query or fragment, percent-decoded, and with
 * `/./`, `//` and a trailing slash folded away, so another spelling of the same path is the same route.
 */
function routeOf(path: string): string {
  let route = path;
  try {
    route = new URL("http://x" + path).pathname; // drops the query and the fragment, resolves `.` and `..`
    route = decodeURIComponent(route);
  } catch {
    // A stray percent sign, or no path at all: the text as far as it could be read.
  }
  return route.replace(/\/{2,}/g, "/").replace(/\/+$/, "");
}

/**
 * Whether sending the request again can never record a second action. Recording is idempotent on `action_id`,
 * and the API may answer 503 `busy` to `POST /v1/actions` after the action itself is stored (its verification
 * then runs in the background). A recording that names no `action_id` would be given a new one by the API on
 * every attempt, so it is never sent twice. `recordAction` always names one; a raw `request` and an inline
 * `verify` carry whatever the caller wrote. Only `dry_run: true` itself counts as a dry run: the API reads the
 * string `"false"` as false and stores the action.
 */
function repeatable(method: string, path: string, body: Json | null | undefined): boolean {
  if (method.toUpperCase() !== "POST" || !body || typeof body !== "object" || Array.isArray(body)) return true;
  const route = routeOf(path);
  if (route === "/v1/actions") return Boolean(body.action_id);
  if (route === "/v1/actions/verify" && body.dry_run !== true) {
    const action = body.action;
    if (action && typeof action === "object" && !Array.isArray(action)) return Boolean((action as Json).action_id);
  }
  return true;
}

/**
 * The body of a request, fixed as JSON text when the call starts: each call of the result gives a new copy of
 * what the body was then, whatever the caller has done to its own objects since. Undefined when the body has no
 * JSON form; such a request is sent once only.
 */
function frozenBody(body: Json | null | undefined): (() => Json | null | undefined) | undefined {
  if (body == null) return () => body;
  try {
    const text: unknown = JSON.stringify(body);
    return typeof text === "string" ? () => JSON.parse(text) as Json : undefined;
  } catch {
    return undefined;
  }
}

function apiError(status: number, json: unknown, retry: { retryAfterMs?: number; attempts?: number }): CommitLayerError {
  const err = (json as { error?: unknown } | null)?.error;
  if (err && typeof err === "object") {
    const e = err as { code?: string; message?: string; details?: unknown };
    return new CommitLayerError(status, e.code ?? "error", e.message ?? "", e.details, retry);
  }
  return new CommitLayerError(status, "error", JSON.stringify(json).slice(0, 300), undefined, retry);
}

export class Client {
  readonly apiKey: string;
  readonly apiUrl: string;
  readonly timeoutMs: number;
  readonly agentId: string | undefined;
  /** Retries after the first attempt for a 429 or 503 answer that carries `Retry-After`; 0 is off. */
  readonly maxRetries: number;
  /** The longest wait before one retry; a longer `Retry-After` is thrown at once. */
  readonly maxRetryWaitMs: number;
  private readonly transport: Transport;
  private readonly onRetry: ((info: RetryInfo) => void) | undefined;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly random: () => number;
  private readonly now: () => number;

  constructor(options: ClientOptions = {}) {
    this.apiKey = options.apiKey || env("COMMITLAYER_API_KEY") || "";
    this.apiUrl = (options.apiUrl || env("COMMITLAYER_API_URL") || "https://api.commitlayer.ai").replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? 20_000;
    this.agentId = options.agentId;
    this.maxRetries = atLeastZero("maxRetries", options.maxRetries, 3);
    this.maxRetryWaitMs = Math.min(atLeastZero("maxRetryWaitMs", options.maxRetryWaitMs, 10_000), MAX_TIMER_MS);
    this.onRetry = options.onRetry;
    this.sleep = options.sleep ?? defaultSleep;
    this.random = options.random ?? Math.random;
    this.now = options.now ?? Date.now;
    this.transport = options.transport ?? ((m, p, b) => this.http(m, p, b));
    if (!this.apiKey && !options.transport) {
      throw new Error("apiKey is required (or set COMMITLAYER_API_KEY)");
    }
  }

  // ---------------- transport ----------------

  private async http(method: string, path: string, body?: Json | null): Promise<TransportResponse> {
    // One controller and one timer per attempt: a retry starts with the whole of timeoutMs.
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
      const { status, headers } = res;
      const raw = await res.text();
      if (!raw) return { status, json: {}, headers };
      try {
        return { status, json: JSON.parse(raw), headers };
      } catch {
        const message = raw.slice(0, 300);
        if (status >= 400) return { status, json: { error: { code: "http_error", message } }, headers };
        throw new CommitLayerError(status, "invalid_response", message);
      }
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * One request, sent again while the API refuses it with 429 or 503 and a `Retry-After` the client will wait
   * for, at most `maxRetries` more times. The body is fixed when the call starts: the first attempt is given
   * `body` itself and every retry a copy of what it was at that moment, so a retry carries the same
   * `action_id`, `requested_at` and parameters as the first attempt even when the caller has changed its own
   * objects during the wait. The API applies an action once per `action_id`, and answers 409 to the same id
   * with a different request. What the transport throws (a network error, a timeout) is never retried: the
   * client cannot know whether that request was applied. A recording that names no `action_id` is sent once
   * only (see `repeatable`).
   */
  private async call(method: string, path: string, body?: Json | null): Promise<Json> {
    // Taken before anything is awaited, so it is the request as the caller made it.
    const again = this.maxRetries > 0 ? frozenBody(body) : undefined;
    let sending = body;
    for (let attempt = 1; ; attempt++) {
      const { status, json, headers } = await this.transport(method, path, sending);
      if (status < 400) return json as Json;
      const retryAfterMs = retryAfterMsOf(headers, this.now());
      const error = apiError(status, json, { retryAfterMs, attempts: attempt });
      if (
        (status !== 429 && status !== 503) ||
        retryAfterMs === undefined ||
        retryAfterMs > this.maxRetryWaitMs ||
        attempt > this.maxRetries ||
        again === undefined
      ) {
        throw error;
      }
      const next = again();
      if (!repeatable(method, path, next)) throw error;
      // Never less than the server stated, never more than the ceiling: the addition is a share of twice the
      // stated wait, or of what is left under the ceiling when that is less.
      const r = this.random();
      const room = Math.min(retryAfterMs * RETRY_SPREAD, this.maxRetryWaitMs - retryAfterMs);
      const waitMs = retryAfterMs + (r > 0 && r <= 1 ? Math.floor(room * r) : 0);
      try {
        this.onRetry?.({
          method,
          path,
          status,
          code: error.code,
          attempt,
          maxRetries: this.maxRetries,
          retryAfterMs,
          waitMs,
        });
      } catch {
        // A logging callback must not decide the outcome of the request.
      }
      await this.sleep(waitMs);
      sending = next;
    }
  }

  // ---------------- actions and facts ----------------

  /**
   * `POST /v1/actions`: record one action. Returns `{action, verification, allowance}`. The verification is
   * provisional until its settlement window closes and every source it reads has caught up.
   */
  async recordAction(contractId: string, subjectId: string, opts: RecordActionOptions = {}): Promise<Json> {
    // Built once, before the first attempt: a retry sends this body again, generated action_id and request time included.
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

  /**
   * `POST /v1/actions/verify`: verify now, by id or inline. `dryRun` stores nothing. An inline `action` that is
   * stored and names no `action_id` is sent once only, never retried.
   */
  async verify(opts: VerifyOptions = {}): Promise<Json> {
    const body: Json = { dry_run: opts.dryRun ?? false };
    if (opts.actionId) body.action_id = opts.actionId;
    if (opts.action) body.action = opts.action;
    if (opts.facts?.length) body.facts = opts.facts;
    if (opts.links?.length) body.links = opts.links;
    return this.call("POST", "/v1/actions/verify", body);
  }

  /**
   * Any endpoint, with the client's auth, error handling and retries: `request("GET", "/v1/actions/x/wait?timeout=30")`.
   * A `POST /v1/actions` body without an `action_id` is sent once only: the API would record a new action on
   * every attempt.
   */
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
   * settled PASS; FAIL routes to recovery; a timeout is the workflow's decision. A poll the API refuses with
   * `Retry-After` is retried like any other request, which can add up to `maxRetries` times `maxRetryWaitMs` to
   * `timeoutMs`.
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

  // ---------------- workflow packs ----------------

  /**
   * The workflow packs (`/v1/packs`): `client.packs.list()`, `get`, `configure`, `dryRun`, `install`, `coverage`,
   * `recorderMapping`. Install creates drafts and never activates; activation stays a person's
   * `POST /v1/contracts/{id}/select`.
   */
  get packs(): Packs {
    return new Packs(this);
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

/** `{workflow_id: {parameter_key: value}}`, as the pack's manifest names them. */
export type PackConfiguration = Record<string, Record<string, unknown>>;

/**
 * `/v1/packs`: the workflow pack library. Reads take any member; `configure` and `dryRun` write nothing and take an
 * analyst; `install` saves the configured contracts as **drafts** (never activates) and needs `confirm: true`.
 */
export class Packs {
  constructor(private readonly client: Client) {}

  /** `GET /v1/packs`: `{items: PackSummary[]}`. */
  list(): Promise<Json> {
    return this.client.request("GET", "/v1/packs");
  }

  /** `GET /v1/packs/{id}`: the summary plus contracts, recorder mapping, fixtures, templates, readme, versions and requirements. */
  get(packId: string): Promise<Json> {
    return this.client.request("GET", `/v1/packs/${encodeURIComponent(packId)}`);
  }

  /** `POST /v1/packs/{id}/configure`: the configured contracts, the values in force and every weakening. Nothing is written. */
  configure(packId: string, configuration: PackConfiguration = {}): Promise<Json> {
    return this.client.request("POST", `/v1/packs/${encodeURIComponent(packId)}/configure`, { configuration });
  }

  /** `POST /v1/packs/{id}/dry-run`: every fixture case through the engine, in a savepoint; nothing persists. */
  dryRun(packId: string, configuration: PackConfiguration = {}, opts: { workflow?: string } = {}): Promise<Json> {
    const body: Json = { configuration };
    if (opts.workflow) body.workflow = opts.workflow;
    return this.client.request("POST", `/v1/packs/${encodeURIComponent(packId)}/dry-run`, body);
  }

  /**
   * `POST /v1/packs/{id}/install`: save the configured contracts as drafts and record the installation. Refused
   * without `confirm: true`; 402 `plan_required` below the pack's minimum plan.
   */
  install(packId: string, configuration: PackConfiguration = {}, opts: { confirm?: boolean } = {}): Promise<Json> {
    return this.client.request("POST", `/v1/packs/${encodeURIComponent(packId)}/install`, {
      configuration,
      confirm: opts.confirm ?? false,
    });
  }

  /** `GET /v1/packs/{id}/coverage`. */
  coverage(packId: string): Promise<Json> {
    return this.client.request("GET", `/v1/packs/${encodeURIComponent(packId)}/coverage`);
  }

  /** `GET /v1/packs/{id}/recorder-mapping`: the installed mapping, contract versions pinned. */
  recorderMapping(packId: string): Promise<Json> {
    return this.client.request("GET", `/v1/packs/${encodeURIComponent(packId)}/recorder-mapping`);
  }
}
