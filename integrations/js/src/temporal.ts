/**
 * CommitLayer for Temporal (TypeScript SDK).
 *
 * Temporal makes the workflow durable. CommitLayer independently verifies the result.
 *
 * Two pieces:
 *
 * 1. `commitlayerActivityInterceptors(options)`: a factory for the worker's `interceptors.activity` list. When a
 *    mapped activity is about to execute, the intent is recorded as a CommitLayer action BEFORE the activity
 *    runs, so the request time precedes every effect. The activity's result is never sent to the API and never
 *    treated as proof: the verdict comes from the systems of record. Retries share one `action_id`
 *    (`temporal:{workflowId}:{activityId}` by default), so an activity that runs three times is one action.
 *
 * 2. `commitlayerVerify(actionRef, { timeoutMs })`: an activity a workflow proxies after its business
 *    activities. It polls `GET /v1/actions/{ref}` until the verification settles, heartbeating between polls,
 *    and returns the verdict. The workflow decides: PASS completes, FAIL compensates, an unsettled verdict past
 *    the timeout is the workflow's call.
 *
 * `@temporalio/activity` is loaded lazily and only for what the worker does not hand over (the current Context
 * when the factory was called without one, `ApplicationFailure` when recording must fail closed), so this module
 * also loads where the Temporal SDK is absent, e.g. in tests.
 *
 * The agent may declare its intent. It may read the verdict. It must never control the independent facts,
 * the governing contract, or the final evidence.
 */

import {
  type ActionBody,
  type ActionClient,
  type ClientOptions,
  type Json,
  type Verdict,
  PathError,
  defaultLog,
  isObject,
  messageOf,
  nowIso,
  pollUntilSettled,
  renderTemplate,
  resolveClient,
  resolvePath,
  scalar,
  sendAction,
  DEFAULT_INTERVAL_MS,
  DEFAULT_TIMEOUT_MS,
} from "./common.js";

export const CHANNEL = "temporal";

// ---------------- the Temporal types this module reads (structural, so the SDK is not a type dependency) ----------------

/** The subset of `Info` (`@temporalio/activity`) the integration reads. */
export interface ActivityInfoLike {
  readonly activityId: string;
  readonly activityType: string;
  readonly attempt: number;
  readonly workflowExecution?: { readonly workflowId: string; readonly runId: string };
  readonly workflowType?: string;
  readonly taskQueue?: string;
}

/** The subset of the activity `Context` the integration uses. The worker's `Context` satisfies it. */
export interface ActivityContextLike {
  readonly info: ActivityInfoLike;
  readonly heartbeat: (details?: unknown) => void;
  readonly sleep?: (ms: number) => Promise<void>;
}

/** `ActivityExecuteInput` from `@temporalio/worker`: the activity's arguments (and headers, unused here). */
export interface ActivityExecuteInputLike {
  readonly args: unknown[];
}

/** `ActivityInboundCallsInterceptor` from `@temporalio/worker`, as this module implements it. */
export interface ActivityInboundInterceptorLike {
  execute<I extends ActivityExecuteInputLike>(input: I, next: (input: I) => Promise<unknown>): Promise<unknown>;
}

/** What one entry of `interceptors.activity` returns. */
export interface ActivityInterceptorsLike {
  inbound: ActivityInboundInterceptorLike;
}

// ---------------- mapping ----------------

/**
 * How one activity type becomes an action. Paths are dotted paths into `{ args, info, workflowId, runId,
 * activityId, activityType, attempt }`: `args.0.orderId` is the first argument's `orderId`, `info.taskQueue` the
 * queue, `workflowId` the workflow id.
 */
export interface ActivityRule {
  contract_id: string;
  /** Pinned contract version. Carried in the body; see `sendAction` in the root export for what the SDK forwards. */
  contract_version?: number;
  action_type?: string;
  /** Path to the subject id, e.g. `args.0.orderId`. Required; an execution where it is missing is not recorded. */
  subject: string;
  subject_kind?: string;
  /** Parameter name to path, e.g. `{ amount: "args.0.amount" }`. Missing values are left out. */
  params?: Record<string, string>;
  /** Template with `{path}` placeholders. Default `temporal:{workflowId}:{activityId}`, one action per activity across retries. */
  action_id?: string;
  /** Path to the action id of the earlier action this activity corrects. Sent only when present. */
  compensates?: string;
}

export interface TemporalMapping {
  /** Default `agent_id` for every recorded action. */
  agent_id?: string;
  /** Default `workflow_id`; when absent the Temporal workflow id is sent. */
  workflow_id?: string;
  /** Activity type (the function name the worker registers) to rule. */
  activities: Record<string, ActivityRule>;
}

export const DEFAULT_ACTION_ID = "temporal:{workflowId}:{activityId}";

/** The call as the body builder sees it. */
export interface ActivityCall {
  args: unknown[];
  info: ActivityInfoLike;
}

export interface BuildTemporalActionOptions {
  agentId?: string;
  /** Overrides the Temporal workflow id as `workflow_id`. */
  workflowId?: string;
  /** Override the request time (UTC `YYYY-MM-DDTHH:MM:SSZ`); defaults to now. */
  requestedAt?: string;
}

/** The object paths and templates resolve against. */
export function templateRoot(call: ActivityCall): Json {
  const { info } = call;
  const root: Json = {
    args: call.args,
    info,
    activityId: info.activityId,
    activityType: info.activityType,
    attempt: info.attempt,
  };
  if (info.workflowExecution) {
    root.workflowId = info.workflowExecution.workflowId;
    root.runId = info.workflowExecution.runId;
  }
  return root;
}

/**
 * Build the `POST /v1/actions` body for one activity execution. Pure: no I/O, no Temporal context. Throws
 * PathError when the subject (or a template placeholder, e.g. the workflow id of a standalone activity) is missing.
 */
export function buildTemporalAction(rule: ActivityRule, call: ActivityCall, opts: BuildTemporalActionOptions = {}): ActionBody {
  const root = templateRoot(call);
  const subject = resolvePath(root, rule.subject);
  if (subject === undefined || subject === null) {
    throw new PathError(`subject path "${rule.subject}" is missing from the activity call`);
  }
  const params: Json = {};
  for (const [name, path] of Object.entries(rule.params ?? {})) {
    const v = resolvePath(root, path);
    if (v !== undefined) params[name] = v;
  }
  params.temporal_activity_id = call.info.activityId;
  params.temporal_attempt = call.info.attempt;

  const body: ActionBody = {
    action_id: renderTemplate(rule.action_id ?? DEFAULT_ACTION_ID, root),
    contract_id: rule.contract_id,
    subject_id: scalar(subject, `subject path "${rule.subject}"`),
    requested_at: opts.requestedAt ?? nowIso(),
    params,
    channel: CHANNEL,
  };
  if (rule.contract_version !== undefined) body.contract_version = rule.contract_version;
  const workflowId = opts.workflowId ?? call.info.workflowExecution?.workflowId;
  if (workflowId !== undefined) body.workflow_id = workflowId;
  if (opts.agentId !== undefined) body.agent_id = opts.agentId;
  if (rule.action_type !== undefined) body.action_type = rule.action_type;
  if (rule.subject_kind !== undefined) body.subject_kind = rule.subject_kind;
  if (rule.compensates !== undefined) {
    const c = resolvePath(root, rule.compensates);
    if (c !== undefined && c !== null) body.compensates = scalar(c, `compensates path "${rule.compensates}"`);
  }
  return body;
}

// ---------------- lazy access to @temporalio/activity ----------------

interface ActivityModuleLike {
  Context: { current(): ActivityContextLike };
  ApplicationFailure: { nonRetryable(message?: string | null, type?: string | null, ...details: unknown[]): Error };
}

let activityModule: Promise<ActivityModuleLike | undefined> | undefined;

function loadActivityModule(): Promise<ActivityModuleLike | undefined> {
  activityModule ??= import("@temporalio/activity").then(
    (m) => m as unknown as ActivityModuleLike,
    () => undefined,
  );
  return activityModule;
}

async function currentContext(): Promise<ActivityContextLike> {
  const m = await loadActivityModule();
  if (!m) throw new Error("@temporalio/activity is not installed and no activity context was given");
  return m.Context.current();
}

/** Thrown in place of `ApplicationFailure` when `@temporalio/activity` cannot be loaded. */
export class RecordingFailure extends Error {
  readonly nonRetryable = true;
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = "CommitLayerRecordingFailure";
  }
}

async function nonRetryableFailure(message: string, cause: unknown): Promise<Error> {
  const m = await loadActivityModule();
  if (!m) return new RecordingFailure(message, cause);
  return m.ApplicationFailure.nonRetryable(message, "CommitLayerRecordingFailed", { reason: messageOf(cause) });
}

// ---------------- the interceptor ----------------

export interface TemporalRecorderOptions extends ClientOptions {
  mapping: TemporalMapping;
  /** Run the activity when recording fails (default true). Otherwise fail it with a non-retryable ApplicationFailure. */
  failOpen?: boolean;
  /** Diagnostics sink; default `console.warn("commitlayer: ...")`. Never receives credentials. */
  log?: (message: string) => void;
  /**
   * Where the activity info comes from, in order: this provider, the `Context` the worker passed to the
   * factory, `Context.current()` from `@temporalio/activity`. Tests inject the first.
   */
  info?: () => ActivityInfoLike;
  /** Override the request time; defaults to now. */
  now?: () => string;
  /** The API response and the body that was sent, after each successful record. */
  onRecorded?: (recorded: Json, body: ActionBody) => void;
}

/** The rule for an activity type, or undefined when it is not mapped. */
export function findActivityRule(mapping: TemporalMapping, activityType: string): ActivityRule | undefined {
  return Object.hasOwn(mapping.activities, activityType) ? mapping.activities[activityType] : undefined;
}

/**
 * Record one activity execution. Returns the body that was sent, or undefined when the activity is unmapped,
 * the subject is missing, or recording failed with `failOpen` on. Throws (non-retryable) when recording failed
 * with `failOpen` off. Never runs the activity: the interceptor does that after this returns.
 */
export async function recordActivity(
  options: TemporalRecorderOptions,
  client: ActionClient,
  info: ActivityInfoLike,
  args: unknown[],
): Promise<ActionBody | undefined> {
  const log = options.log ?? defaultLog;
  const rule = findActivityRule(options.mapping, info.activityType);
  if (!rule) return undefined;

  let body: ActionBody;
  try {
    body = buildTemporalAction(rule, { args, info }, {
      agentId: options.mapping.agent_id,
      workflowId: options.mapping.workflow_id,
      requestedAt: options.now?.(),
    });
  } catch (e) {
    if (!(e instanceof PathError)) throw e;
    log(`activity ${info.activityType}: ${e.message}: run without recording`);
    return undefined;
  }

  try {
    const recorded = await sendAction(client, body);
    options.onRecorded?.(recorded, body);
    return body;
  } catch (e) {
    const reason = messageOf(e);
    if (options.failOpen ?? true) {
      log(`recording failed for ${body.action_id} (${info.activityType}): ${reason}: activity runs anyway`);
      return undefined;
    }
    log(`recording failed for ${body.action_id} (${info.activityType}): ${reason}: activity refused (fail-open is off)`);
    throw await nonRetryableFailure(`CommitLayer recording failed for ${body.action_id}: ${reason}`, e);
  }
}

/**
 * The factory for `WorkerOptions.interceptors.activity`:
 *
 *     const worker = await Worker.create({
 *       ...,
 *       interceptors: { activity: [commitlayerActivityInterceptors({ mapping })] },
 *     });
 *
 * The client is built on first use from the options or `COMMITLAYER_API_KEY`. For the deprecated
 * `interceptors.activityInbound` list use `(ctx) => commitlayerActivityInterceptors(opts)(ctx).inbound`.
 */
export function commitlayerActivityInterceptors(
  options: TemporalRecorderOptions,
): (ctx?: ActivityContextLike) => ActivityInterceptorsLike {
  let client: ActionClient | undefined;
  const getClient = () => (client ??= resolveClient(options));
  return (ctx?: ActivityContextLike) => ({
    inbound: {
      async execute(input, next) {
        const info = options.info?.() ?? ctx?.info ?? (await currentContext()).info;
        await recordActivity(options, getClient(), info, input.args);
        return next(input); // the result is the activity's own business; it is never sent to CommitLayer
      },
    },
  });
}

// ---------------- the verifier activity ----------------

export interface VerifyActivityOptions extends ClientOptions {
  /** Hold each poll on the completion gate for up to this long (default: the interval); 0 polls plain reads. */
  holdMs?: number;
  /** Where the activity context comes from; default `Context.current()`. Tests inject a fake. */
  context?: () => ActivityContextLike;
  /** Default polling interval between `GET /v1/actions/{ref}` calls; default 5s. */
  intervalMs?: number;
  /** Default timeout; default 15 minutes. */
  timeoutMs?: number;
  /** Clock in ms; injectable for tests. */
  now?: () => number;
}

export interface VerifyOptions {
  /** Hold each poll on the completion gate for up to this long (default: the interval); 0 polls plain reads. */
  holdMs?: number;
  /** How long to wait for settlement. Past it the provisional verdict is returned with `timedOut: true`. */
  timeoutMs?: number;
  intervalMs?: number;
}

export interface CommitLayerActivities {
  /**
   * Wait for the verification of `actionRef` (an `action_id`, e.g. `temporal:<workflowId>:<activityId>`) to settle
   * and return the verdict. Heartbeats after every poll and sleeps through the activity context, so cancellation
   * and heartbeat timeouts work as for any activity. Give it a `startToCloseTimeout` above `timeoutMs` and a
   * `heartbeatTimeout` above the interval.
   */
  commitlayerVerify(actionRef: string, opts?: VerifyOptions): Promise<Verdict>;
}

/** The activities to register on the worker, bound to a client. */
export function createCommitLayerActivities(options: VerifyActivityOptions = {}): CommitLayerActivities {
  let client: ActionClient | undefined;
  const getClient = () => (client ??= resolveClient(options));
  return {
    async commitlayerVerify(actionRef: string, opts: VerifyOptions = {}): Promise<Verdict> {
      const ctx = options.context ? options.context() : await currentContext();
      return pollUntilSettled(getClient(), actionRef, {
        timeoutMs: opts.timeoutMs ?? options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        intervalMs: opts.intervalMs ?? options.intervalMs ?? DEFAULT_INTERVAL_MS,
        sleep: ctx.sleep ? (ms) => ctx.sleep!(ms) : undefined,
        now: options.now,
        holdMs: opts.holdMs ?? options.holdMs ?? opts.intervalMs ?? options.intervalMs ?? DEFAULT_INTERVAL_MS,
        onPoll: (verdict, polls) => ctx.heartbeat({ polls, status: verdict.status, outcome: verdict.outcome, policy: verdict.policy }),
      });
    },
  };
}

/** `commitlayerVerify` configured from `COMMITLAYER_API_KEY` / `COMMITLAYER_API_URL` on first call. */
export const { commitlayerVerify } = createCommitLayerActivities();

/** Validate a mapping loaded from JSON (a config file, an environment variable). Throws on a malformed document. */
export function parseTemporalMapping(raw: unknown, source = "mapping"): TemporalMapping {
  if (!isObject(raw)) throw new Error(`${source}: the mapping must be an object`);
  if (!isObject(raw.activities)) throw new Error(`${source}: activities must be an object of activity type to rule`);
  const mapping: TemporalMapping = { activities: {} };
  if (raw.agent_id !== undefined) mapping.agent_id = String(raw.agent_id);
  if (raw.workflow_id !== undefined) mapping.workflow_id = String(raw.workflow_id);
  for (const [name, rule] of Object.entries(raw.activities)) {
    if (!isObject(rule)) throw new Error(`${source}: activities.${name} must be an object`);
    if (typeof rule.contract_id !== "string" || !rule.contract_id) throw new Error(`${source}: activities.${name}.contract_id is required`);
    if (typeof rule.subject !== "string" || !rule.subject) throw new Error(`${source}: activities.${name}.subject is required`);
    const parsed: ActivityRule = { contract_id: rule.contract_id, subject: rule.subject };
    if (rule.contract_version !== undefined) {
      if (typeof rule.contract_version !== "number" || !Number.isInteger(rule.contract_version)) {
        throw new Error(`${source}: activities.${name}.contract_version must be an integer`);
      }
      parsed.contract_version = rule.contract_version;
    }
    for (const key of ["action_type", "subject_kind", "action_id", "compensates"] as const) {
      const v = rule[key];
      if (v === undefined || v === null) continue;
      if (typeof v !== "string") throw new Error(`${source}: activities.${name}.${key} must be a string`);
      parsed[key] = v;
    }
    if (rule.params !== undefined && rule.params !== null) {
      if (!isObject(rule.params)) throw new Error(`${source}: activities.${name}.params must be an object of name to path`);
      const params: Record<string, string> = {};
      for (const [k, v] of Object.entries(rule.params)) {
        if (typeof v !== "string" || !v) throw new Error(`${source}: activities.${name}.params.${k} must be a path string`);
        params[k] = v;
      }
      parsed.params = params;
    }
    mapping.activities[name] = parsed;
  }
  return mapping;
}
