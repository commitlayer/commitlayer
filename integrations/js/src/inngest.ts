/**
 * CommitLayer for Inngest (TypeScript SDK).
 *
 * Inngest makes the function durable. CommitLayer independently verifies the result.
 *
 * - `recordedStep(step, "name", fn, options)`: records the intent as a CommitLayer action in its own memoised
 *   step (`<name>:commitlayer`) and then runs `step.run("name", fn)`. Replays never re-record: the record step is
 *   memoised like any other. A retry of `fn` does not re-record either, so `requested_at` stays before the first
 *   attempt's effects. The step's result is never sent to the API: the verdict comes from the systems of record.
 *
 * - `waitForVerdict(step, actionRef, { timeoutMs })`: polls `GET /v1/actions/{ref}` in `step.run` steps with
 *   `step.sleep` between them until the verification settles or the timeout passes, and returns the verdict.
 *   The function decides: PASS completes, FAIL compensates, an unsettled verdict past the timeout is its call.
 *
 * Nothing is imported from `inngest` at runtime; the helpers take the `step` tools the handler receives.
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
  defaultLog,
  messageOf,
  nowIso,
  resolveClient,
  sendAction,
  summarizeVerification,
  DEFAULT_INTERVAL_MS,
  DEFAULT_TIMEOUT_MS,
  fetchVerification,
} from "./common.js";

export const CHANNEL = "inngest";

/** The step tools this module uses. The `step` an Inngest handler receives satisfies it. */
export interface StepLike {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  run: (id: string, fn: () => any) => Promise<any>;
  sleep: (id: string, time: number | string) => Promise<void>;
}

// ---------------- recordedStep ----------------

export interface RecordedStepOptions extends ClientOptions {
  contract_id: string;
  subject_id: string | number;
  /** The parameters the contract limits (an amount, a tier). `inngest_run_id` and `inngest_step_id` are added. */
  params?: Json;
  /** Default `inngest:{runId}:{stepId}`: one action per step per run, whatever Inngest retries. */
  action_id?: string;
  action_type?: string;
  subject_kind?: string;
  /** Default `functionId`. */
  workflow_id?: string;
  agent_id?: string;
  /** The action id of an earlier action this step corrects. */
  compensates?: string;
  /** Pinned contract version. Carried in the body; see `sendAction` in the root export for what the SDK forwards. */
  contract_version?: number;
  /** The `runId` the handler received. Part of the default `action_id` and of `params`. */
  runId: string;
  /** The Inngest function id; the default `workflow_id`. */
  functionId?: string;
  /** Run the step when recording fails (default true). Otherwise the record step throws and Inngest retries it. */
  failOpen?: boolean;
  /** Diagnostics sink; default `console.warn("commitlayer: ...")`. Never receives credentials. */
  log?: (message: string) => void;
  /** Override the request time; defaults to now. */
  now?: () => string;
  /** The API response and the body that was sent, after a successful record (not on replays). */
  onRecorded?: (recorded: Json, body: ActionBody) => void;
}

/** Build the `POST /v1/actions` body for one step. Pure: no I/O. */
export function buildInngestAction(stepId: string, options: RecordedStepOptions): ActionBody {
  const params: Json = { ...(options.params ?? {}) };
  params.inngest_run_id = options.runId;
  params.inngest_step_id = stepId;
  const body: ActionBody = {
    action_id: options.action_id ?? `inngest:${options.runId}:${stepId}`,
    contract_id: options.contract_id,
    subject_id: String(options.subject_id),
    requested_at: options.now?.() ?? nowIso(),
    params,
    channel: CHANNEL,
  };
  if (options.contract_version !== undefined) body.contract_version = options.contract_version;
  const workflowId = options.workflow_id ?? options.functionId;
  if (workflowId !== undefined) body.workflow_id = workflowId;
  if (options.agent_id !== undefined) body.agent_id = options.agent_id;
  if (options.action_type !== undefined) body.action_type = options.action_type;
  if (options.subject_kind !== undefined) body.subject_kind = options.subject_kind;
  if (options.compensates !== undefined) body.compensates = options.compensates;
  return body;
}

/** What the record step memoises. */
export interface RecordStepResult {
  recorded: boolean;
  action_id: string;
  /** Why recording failed, when it did and `failOpen` let the step run. */
  error?: string;
}

/** The id of the record step that `recordedStep` runs before `stepId`. */
export function recordStepId(stepId: string): string {
  return `${stepId}:commitlayer`;
}

/**
 * Record the step's intent as an action, then run the step. Returns what `step.run(name, fn)` returns.
 *
 *     const refund = await recordedStep(step, "refund", () => payments.refund(orderId, amount), {
 *       runId, functionId: "order-refund",
 *       contract_id: "refund_workflow", subject_id: orderId, subject_kind: "order", params: { amount },
 *     });
 *
 * The action id is `inngest:<runId>:<name>` unless `action_id` is given, so the record is idempotent across
 * retries of the record step and the API updates one action instead of creating two.
 */
export async function recordedStep<T>(
  step: StepLike,
  name: string,
  fn: () => Promise<T> | T,
  options: RecordedStepOptions,
): Promise<T> {
  const log = options.log ?? defaultLog;
  const body = buildInngestAction(name, options);
  await step.run(recordStepId(name), async (): Promise<RecordStepResult> => {
    try {
      const recorded = await sendAction(resolveClient(options), body);
      options.onRecorded?.(recorded, body);
      return { recorded: true, action_id: body.action_id };
    } catch (e) {
      const reason = messageOf(e);
      if (options.failOpen ?? true) {
        log(`recording failed for ${body.action_id} (${name}): ${reason}: step runs anyway`);
        return { recorded: false, action_id: body.action_id, error: reason };
      }
      log(`recording failed for ${body.action_id} (${name}): ${reason}: step refused (fail-open is off)`);
      throw e;
    }
  });
  return (await step.run(name, fn)) as T; // the result is the step's own business; it is never sent to CommitLayer
}

// ---------------- waitForVerdict ----------------

export interface WaitForVerdictOptions extends ClientOptions {
  /** How long to wait for settlement. Past it the provisional verdict is returned with `timedOut: true`. Default 15 minutes. */
  timeoutMs?: number;
  /** `step.sleep` between polls. Default 5 seconds. */
  intervalMs?: number;
  /** Prefix of the step ids (`<prefix>:poll:<n>`, `<prefix>:sleep:<n>`). Default `commitlayer:<actionRef>`. */
  stepId?: string;
  /** Hold each poll on the completion gate for up to this long (default: the interval); 0 polls plain reads. */
  holdMs?: number;
}

/**
 * Wait for the verification of `actionRef` (an `action_id`, e.g. `inngest:<runId>:<step>`) to settle. Every
 * poll is a `step.run` and every pause a `step.sleep`, so the function is suspended between polls and a replay
 * walks the memoised polls without calling the API again. The number of polls is derived from `timeoutMs` and
 * `intervalMs`, not from the wall clock, so it is the same on every replay.
 */
export async function waitForVerdict(step: StepLike, actionRef: string, options: WaitForVerdictOptions = {}): Promise<Verdict> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const intervalMs = Math.max(1, options.intervalMs ?? DEFAULT_INTERVAL_MS);
  const prefix = options.stepId ?? `commitlayer:${actionRef}`;
  let client: ActionClient | undefined;
  for (let i = 0; ; i++) {
    const raw = (await step.run(`${prefix}:poll:${i}`, () =>
      fetchVerification((client ??= resolveClient(options)), actionRef, options.holdMs ?? intervalMs),
    )) as Json;
    const verdict = summarizeVerification(raw);
    if (verdict.settled) return verdict;
    if ((i + 1) * intervalMs > timeoutMs) return { ...verdict, timedOut: true };
    await step.sleep(`${prefix}:sleep:${i}`, intervalMs);
  }
}
