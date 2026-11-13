/**
 * Shared pieces of `@commitlayer/integrations`: the action body, path and template helpers, the client seam,
 * the verdict summary and the generic settle-polling loop. The workflow-engine subpaths build on these.
 *
 * The agent may declare its intent. It may read the verdict. It must never control the independent facts,
 * the governing contract, or the final evidence.
 */

import {
  Client,
  CommitLayerError,
  type ClientOptions as SdkClientOptions,
  type Json,
  type RecordActionOptions,
} from "commitlayer";

export const VERSION = "0.1.4";

export type { Json, RecordActionOptions };
export { Client, CommitLayerError };

/** What the integrations need from a CommitLayer client. `Client` from `commitlayer` satisfies it. */
export interface ActionClient {
  recordAction(contractId: string, subjectId: string, opts?: RecordActionOptions): Promise<Json>;
  getAction(ref: string): Promise<Json>;
  /** Any endpoint; used for one held call on the completion gate (`GET /v1/actions/{ref}/wait`). */
  request?(method: string, path: string, body?: Json | null): Promise<Json>;
}

/** Either a ready client or the options to build one (`COMMITLAYER_API_KEY` / `COMMITLAYER_API_URL` when omitted). */
export interface ClientOptions {
  client?: ActionClient;
  apiKey?: string;
  apiUrl?: string;
  /** Per-request timeout of the client built here; default 20s. */
  timeoutMs?: number;
  /**
   * How many times the client built here sends a request again after the API refused it with 429 or 503 and a
   * `Retry-After` header; 0 turns it off. Each integration sets its own default: a record in front of an
   * activity or a step retries twice, a verdict poll does not retry (the workflow engine retries it). Needs
   * `commitlayer` 0.1.4 or later: an earlier SDK ignores it and never retries. A `client` passed in keeps
   * its own settings.
   */
  maxRetries?: number;
  /** The longest wait before one such retry, in milliseconds. A longer `Retry-After` is an error at once. */
  maxRetryWaitMs?: number;
}

/** The retry settings an integration gives the client it builds when the caller sets none. */
export interface RetrySettings {
  maxRetries?: number;
  maxRetryWaitMs?: number;
}

/**
 * For a record that sits in front of the caller's own timed work (an activity, a step): two retries of at most
 * two seconds each, so the record adds four seconds of waiting at most. The API's busy answers state one second.
 */
export const INLINE_RETRY: Readonly<RetrySettings> = Object.freeze({ maxRetries: 2, maxRetryWaitMs: 2_000 });

/** For a call the workflow engine retries itself (a verdict poll in an activity or a step): the SDK does not. */
export const NO_RETRY: Readonly<RetrySettings> = Object.freeze({ maxRetries: 0 });

/**
 * The options the SDK's `Client` is built with: the caller's own, then the integration's retry settings for
 * what the caller left unset. Without either the SDK's defaults apply (3 retries, 10 seconds).
 */
export function sdkClientOptions(opts: ClientOptions, retry: Readonly<RetrySettings> = {}): SdkClientOptions & RetrySettings {
  const out: SdkClientOptions & RetrySettings = { apiKey: opts.apiKey, apiUrl: opts.apiUrl, timeoutMs: opts.timeoutMs };
  const maxRetries = opts.maxRetries ?? retry.maxRetries;
  const maxRetryWaitMs = opts.maxRetryWaitMs ?? retry.maxRetryWaitMs;
  if (maxRetries !== undefined) out.maxRetries = maxRetries;
  if (maxRetryWaitMs !== undefined) out.maxRetryWaitMs = maxRetryWaitMs;
  return out;
}

/** The client from the options, or a new `Client` with the given retry settings. Throws when no key is available. */
export function resolveClient(opts: ClientOptions, retry?: Readonly<RetrySettings>): ActionClient {
  if (opts.client) return opts.client;
  return new Client(sdkClientOptions(opts, retry));
}

/** The body of `POST /v1/actions` (ActionIn in docs/API.md). */
export interface ActionBody {
  action_id: string;
  contract_id: string;
  contract_version?: number;
  workflow_id?: string;
  agent_id?: string;
  action_type?: string;
  subject_id: string;
  subject_kind?: string;
  requested_at: string;
  params: Json;
  channel: string;
  compensates?: string;
}

/** Record a built body through the client. */
export function sendAction(client: ActionClient, body: ActionBody): Promise<Json> {
  const opts: RecordActionOptions = {
    actionId: body.action_id,
    params: body.params,
    requestedAt: body.requested_at,
    channel: body.channel,
  };
  if (body.action_type !== undefined) opts.actionType = body.action_type;
  if (body.subject_kind !== undefined) opts.subjectKind = body.subject_kind;
  if (body.workflow_id !== undefined) opts.workflowId = body.workflow_id;
  if (body.agent_id !== undefined) opts.agentId = body.agent_id;
  if (body.compensates !== undefined) opts.compensates = body.compensates;
  if (body.contract_version !== undefined) opts.contractVersion = body.contract_version;
  return client.recordAction(body.contract_id, body.subject_id, opts);
}

// ---------------- paths and templates ----------------

/** A path the mapping needs (the subject, or a template placeholder) is missing from the call. */
export class PathError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PathError";
  }
}

export function isObject(v: unknown): v is Json {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/** Walk a dotted path (`args.0.orderId`, `info.attempt`); undefined when any segment is missing. */
export function resolvePath(root: unknown, path: string): unknown {
  let cur: unknown = root;
  for (const seg of path.split(".")) {
    if (cur === null || typeof cur !== "object" || !Object.hasOwn(cur, seg)) return undefined;
    cur = (cur as Record<string, unknown>)[seg];
  }
  return cur;
}

/** A string, number or boolean as a string; anything else is a PathError. */
export function scalar(value: unknown, what: string): string {
  switch (typeof value) {
    case "string":
      return value;
    case "number":
    case "boolean":
    case "bigint":
      return String(value);
    default:
      throw new PathError(`${what} must be a string, number or boolean, got ${Array.isArray(value) ? "array" : typeof value}`);
  }
}

/** Fill `{path}` placeholders from `root`. Throws PathError when a placeholder's path is missing. */
export function renderTemplate(template: string, root: unknown): string {
  return template.replace(/\{([^{}]+)\}/g, (_m, p: string) => {
    const path = p.trim();
    const v = resolvePath(root, path);
    if (v === undefined || v === null) throw new PathError(`template path "${path}" is missing`);
    return scalar(v, `template path "${path}"`);
  });
}

/** UTC to the second, as the API stores it. */
export function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function messageOf(e: unknown): string {
  if (e instanceof CommitLayerError) return e.toString();
  if (e instanceof Error) return e.message;
  return String(e);
}

/** Default diagnostics sink. Messages carry action ids and error codes, never credentials. */
export function defaultLog(message: string): void {
  console.warn(`commitlayer: ${message}`);
}

// ---------------- verdicts ----------------

export type VerdictLabel = "PASS" | "FAIL" | "UNKNOWN";

/** A workflow-sized view of an ActionVerification (`GET /v1/actions/{ref}`). */
export interface Verdict {
  /** The `action_id` the action was recorded under. */
  actionId: string | null;
  /** `settled` once the window closed and every source caught up (or the outcome lens passed early). */
  status: "pending" | "settled";
  settled: boolean;
  /** True when the wait gave up before settlement; `outcome` / `policy` are then the provisional verdicts. */
  timedOut: boolean;
  /** Did every expected effect happen? */
  outcome: VerdictLabel | null;
  /** ...and were the preconditions, limits and prohibitions respected? */
  policy: VerdictLabel | null;
  reasons: { outcome: string[]; policy: string[] };
  reasonLabels: { outcome: string[]; policy: string[] };
  headline: string | null;
  settlesAt: string | null;
  /** The full verification as the API returned it (checks, evidence, facts). */
  verification: Json;
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function label(v: unknown): VerdictLabel | null {
  return v === "PASS" || v === "FAIL" || v === "UNKNOWN" ? v : null;
}

/** Reduce a verification response to the fields a workflow decides on. */
export function summarizeVerification(verification: Json, timedOut = false): Verdict {
  const action = isObject(verification.action) ? verification.action : {};
  const lenses = isObject(verification.lenses) ? verification.lenses : {};
  const outcome = isObject(lenses.outcome) ? lenses.outcome : {};
  const policy = isObject(lenses.policy) ? lenses.policy : {};
  const summary = isObject(verification.summary) ? verification.summary : {};
  const status = verification.status === "settled" ? "settled" : "pending";
  return {
    actionId: typeof action.action_id === "string" ? action.action_id : null,
    status,
    settled: status === "settled",
    timedOut,
    outcome: label(outcome.verdict),
    policy: label(policy.verdict),
    reasons: { outcome: strings(outcome.reason_codes), policy: strings(policy.reason_codes) },
    reasonLabels: { outcome: strings(outcome.reason_labels), policy: strings(policy.reason_labels) },
    headline: typeof summary.headline === "string" ? summary.headline : null,
    settlesAt: typeof verification.settles_at === "string" ? verification.settles_at : null,
    verification,
  };
}

export interface PollOptions {
  /** How long to wait for settlement before returning the provisional verdict with `timedOut: true`. Default 15 minutes. */
  timeoutMs?: number;
  /** Pause between polls. Default 5 seconds. */
  intervalMs?: number;
  /** The pause itself; injectable (an activity context's cancellation-aware `sleep`, a fake in tests). */
  sleep?: (ms: number) => Promise<void>;
  /** Called after every poll, e.g. to heartbeat. */
  onPoll?: (verdict: Verdict, polls: number) => void;
  /** Clock in ms; injectable for tests. */
  now?: () => number;
  /**
   * Hold each poll on the API's completion gate (`GET /v1/actions/{ref}/wait?timeout=`) for up to this long
   * (60 s at most), so a poll returns the moment the verdict settles and no local pause is needed between polls.
   * 0 polls with plain `GET /v1/actions/{ref}` and pauses `intervalMs` between polls.
   */
  holdMs?: number;
}

/** One read of the verification: through the gate when `holdMs` is set and the client supports it, else a plain GET. */
export function fetchVerification(client: ActionClient, actionRef: string, holdMs = 0): Promise<Json> {
  const hold = Math.max(0, Math.min(60, Math.round(holdMs / 1000)));
  if (hold > 0 && client.request) return client.request("GET", `/v1/actions/${actionRef}/wait?timeout=${hold}`);
  return client.getAction(actionRef);
}

export const DEFAULT_TIMEOUT_MS = 15 * 60_000;
export const DEFAULT_INTERVAL_MS = 5_000;

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll `GET /v1/actions/{ref}` until the verification is `settled` or `timeoutMs` has passed. A transport
 * error propagates (the caller's retry policy applies); a timeout does not throw, it returns the latest
 * provisional verdict with `timedOut: true`, because what to do with an unsettled action is the workflow's decision.
 */
export async function pollUntilSettled(client: ActionClient, actionRef: string, opts: PollOptions = {}): Promise<Verdict> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const intervalMs = Math.max(1, opts.intervalMs ?? DEFAULT_INTERVAL_MS);
  const sleep = opts.sleep ?? defaultSleep;
  const now = opts.now ?? Date.now;
  const held = Boolean(opts.holdMs && opts.holdMs >= 1000 && client.request);
  const start = now();
  let polls = 0;
  for (;;) {
    const remainingBefore = timeoutMs - (now() - start);
    const verdict = summarizeVerification(
      await fetchVerification(client, actionRef, held ? Math.min(opts.holdMs!, Math.max(1, remainingBefore)) : 0),
    );
    polls += 1;
    opts.onPoll?.(verdict, polls);
    if (verdict.settled) return verdict;
    const remaining = timeoutMs - (now() - start);
    if (remaining <= 0) return { ...verdict, timedOut: true };
    if (!held) await sleep(Math.min(intervalMs, remaining));
  }
}
