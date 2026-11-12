/**
 * CommitLayer MCP Recorder: a stdio proxy between an MCP client and any downstream MCP server.
 *
 * Every JSON-RPC message is forwarded unchanged in both directions. When the client calls a mapped tool
 * (`tools/call`), the intent is recorded as a CommitLayer action BEFORE the call is forwarded, so the request
 * time precedes every effect. The downstream result is never sent to the API and never treated as proof:
 * CommitLayer verifies from the systems of record.
 *
 * Recording is fail-open by default: when the API cannot be reached the call is forwarded anyway and a line
 * goes to stderr. The proxy never blocks, reorders or alters the agent's traffic; the record for one line
 * completes (or fails) before that line is forwarded, and later lines queue behind it.
 *
 * The agent may declare its intent. It may read the verdict. It must never control the independent facts,
 * the governing contract, or the final evidence.
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";

export const VERSION = "0.1.3";

export type Json = Record<string, unknown>;

/** A JSON-RPC 2.0 request id. */
export type JsonRpcId = string | number | null;

/** One tool's entry in the mapping file. Paths are dotted paths into the `tools/call` params object. */
export interface ToolRule {
  contract_id: string;
  contract_version?: number;
  action_type?: string;
  /**
   * Path to the subject id, e.g. `arguments.order_id`, or a template with `{path}` placeholders such as
   * `zendesk_ticket_{arguments.ticket_id}`. Required; a call where it is missing is not recorded.
   */
  subject: string;
  subject_kind?: string;
  /** Parameter name to path, e.g. `{ "amount": "arguments.amount" }`. Missing values are left out. */
  params?: Record<string, string>;
  /** Template with `{path}` placeholders, e.g. `refund:{arguments.order_id}`. Default `mcp:{tool}:{request id}`. */
  action_id?: string;
  /** Path to the id of the earlier action this call corrects. Sent only when present. */
  compensates?: string;
}

export interface Mapping {
  agent_id?: string;
  workflow_id?: string;
  /** Tool name (optionally with a server prefix, `stripe.refund_payment`) to rule. */
  tools: Record<string, ToolRule>;
}

/** The body sent to `POST /v1/actions` (ActionIn). */
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
  channel: "mcp";
  compensates?: string;
}

/** The mapping file is malformed. */
export class MappingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MappingError";
  }
}

/** A path the mapping needs (the subject, or a template placeholder) is missing from the call. */
export class PathError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PathError";
  }
}

// ---------------- mapping ----------------

const RULE_KEYS = new Set([
  "contract_id",
  "contract_version",
  "action_type",
  "subject",
  "subject_kind",
  "params",
  "action_id",
  "compensates",
]);

function isObject(v: unknown): v is Json {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function optionalString(obj: Json, key: string, where: string): string | undefined {
  const v = obj[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "string") throw new MappingError(`${where}.${key} must be a string`);
  return v;
}

function requiredString(obj: Json, key: string, where: string): string {
  const v = obj[key];
  if (v === undefined || v === null || v === "") throw new MappingError(`${where}.${key} is required`);
  if (typeof v !== "string") throw new MappingError(`${where}.${key} must be a string`);
  return v;
}

function parseRule(name: string, raw: unknown, where: string): ToolRule {
  if (!isObject(raw)) throw new MappingError(`${where} must be an object`);
  for (const key of Object.keys(raw)) {
    if (!RULE_KEYS.has(key)) throw new MappingError(`${where}.${key} is not a known key`);
  }
  const rule: ToolRule = {
    contract_id: requiredString(raw, "contract_id", where),
    subject: requiredString(raw, "subject", where),
  };
  const version = raw.contract_version;
  if (version !== undefined && version !== null) {
    if (typeof version !== "number" || !Number.isInteger(version)) {
      throw new MappingError(`${where}.contract_version must be an integer`);
    }
    rule.contract_version = version;
  }
  for (const key of ["action_type", "subject_kind", "action_id", "compensates"] as const) {
    const v = optionalString(raw, key, where);
    if (v !== undefined) rule[key] = v;
  }
  if (raw.params !== undefined && raw.params !== null) {
    if (!isObject(raw.params)) throw new MappingError(`${where}.params must be an object of name to path`);
    const params: Record<string, string> = {};
    for (const [k, v] of Object.entries(raw.params)) {
      if (typeof v !== "string" || v === "") throw new MappingError(`${where}.params.${k} must be a path string`);
      params[k] = v;
    }
    rule.params = params;
  }
  if (name === "") throw new MappingError(`${where}: tool names must not be empty`);
  return rule;
}

/** Validate a parsed mapping document. `source` names it in error messages. */
export function parseMapping(raw: unknown, source = "mapping"): Mapping {
  if (!isObject(raw)) throw new MappingError(`${source}: the mapping must be a JSON object`);
  for (const key of Object.keys(raw)) {
    if (key !== "agent_id" && key !== "workflow_id" && key !== "tools") {
      throw new MappingError(`${source}: ${key} is not a known key`);
    }
  }
  if (!isObject(raw.tools)) throw new MappingError(`${source}: tools must be an object of tool name to rule`);
  const mapping: Mapping = { tools: {} };
  const agentId = optionalString(raw, "agent_id", source);
  if (agentId !== undefined) mapping.agent_id = agentId;
  const workflowId = optionalString(raw, "workflow_id", source);
  if (workflowId !== undefined) mapping.workflow_id = workflowId;
  for (const [name, rule] of Object.entries(raw.tools)) {
    mapping.tools[name] = parseRule(name, rule, `${source}: tools.${name}`);
  }
  return mapping;
}

/** Read and validate a mapping file. */
export function loadMapping(file: string): Mapping {
  let text: string;
  try {
    text = readFileSync(file, "utf8");
  } catch (e) {
    throw new MappingError(`cannot read mapping file ${file}: ${messageOf(e)}`);
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    throw new MappingError(`${file}: not valid JSON: ${messageOf(e)}`);
  }
  return parseMapping(raw, file);
}

/**
 * The rule for a tool name: an exact key, or a key carrying a server prefix (`stripe.refund_payment` matches
 * `refund_payment`). Returns undefined for an unmapped tool.
 */
export function findTool(mapping: Mapping, name: string): ToolRule | undefined {
  if (Object.hasOwn(mapping.tools, name)) return mapping.tools[name];
  for (const key of Object.keys(mapping.tools)) {
    if (key.endsWith("." + name)) return mapping.tools[key];
  }
  return undefined;
}

// ---------------- paths and templates ----------------

/** Walk a dotted path (`arguments.order_id`, `items.0.id`); undefined when any segment is missing. */
export function resolvePath(root: unknown, path: string): unknown {
  let cur: unknown = root;
  for (const seg of path.split(".")) {
    if (cur === null || typeof cur !== "object" || !Object.hasOwn(cur, seg)) return undefined;
    cur = (cur as Record<string, unknown>)[seg];
  }
  return cur;
}

function scalar(value: unknown, what: string): string {
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

/** Fill `{path}` placeholders from the call. Throws PathError when a placeholder's path is missing. */
export function renderTemplate(template: string, root: unknown): string {
  return template.replace(/\{([^{}]+)\}/g, (_m, p: string) => {
    const path = p.trim();
    const v = resolvePath(root, path);
    if (v === undefined || v === null) throw new PathError(`template path "${path}" is missing from the call`);
    return scalar(v, `template path "${path}"`);
  });
}

export interface BuildActionOptions {
  /** The JSON-RPC id of the `tools/call` request. */
  requestId?: JsonRpcId;
  agentId?: string;
  workflowId?: string;
  /** Override the request time (UTC `YYYY-MM-DDTHH:MM:SSZ`); defaults to now. */
  requestedAt?: string;
}

function now(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * Build the `POST /v1/actions` body for one `tools/call` from its rule and the call's params object
 * (`{ name, arguments, _meta }`). Throws PathError when the subject (or a template placeholder) is missing.
 */
export function buildAction(rule: ToolRule, callParams: unknown, opts: BuildActionOptions = {}): ActionBody {
  const call: Json = isObject(callParams) ? callParams : {};
  const tool = typeof call.name === "string" ? call.name : String(call.name ?? "");
  // A template such as `zendesk_ticket_{arguments.ticket_id}` spells out a prefix the tool call does not carry;
  // a plain path is resolved as is.
  let subjectId: string;
  if (rule.subject.includes("{")) {
    subjectId = renderTemplate(rule.subject, call);
  } else {
    const subject = resolvePath(call, rule.subject);
    if (subject === undefined || subject === null) {
      throw new PathError(`subject path "${rule.subject}" is missing from the call`);
    }
    subjectId = scalar(subject, `subject path "${rule.subject}"`);
  }
  const requestId = opts.requestId === undefined || opts.requestId === null ? crypto.randomUUID() : String(opts.requestId);

  const params: Json = {};
  for (const [name, path] of Object.entries(rule.params ?? {})) {
    const v = resolvePath(call, path);
    if (v !== undefined) params[name] = v;
  }
  params.mcp_tool = tool;
  params.mcp_request_id = requestId;

  const body: ActionBody = {
    action_id: rule.action_id ? renderTemplate(rule.action_id, call) : `mcp:${tool}:${requestId}`,
    contract_id: rule.contract_id,
    subject_id: subjectId,
    requested_at: opts.requestedAt ?? now(),
    params,
    channel: "mcp",
  };
  if (rule.contract_version !== undefined) body.contract_version = rule.contract_version;
  if (opts.workflowId) body.workflow_id = opts.workflowId;
  if (opts.agentId) body.agent_id = opts.agentId;
  if (rule.action_type) body.action_type = rule.action_type;
  if (rule.subject_kind) body.subject_kind = rule.subject_kind;
  if (rule.compensates) {
    const c = resolvePath(call, rule.compensates);
    if (c !== undefined && c !== null) body.compensates = scalar(c, `compensates path "${rule.compensates}"`);
  }
  return body;
}

// ---------------- the recorder ----------------

export interface RecorderOptions {
  mapping: Mapping;
  /** Without a key the recorder warns once and forwards everything without recording. */
  apiKey?: string;
  /** Default https://api.commitlayer.ai. */
  apiUrl?: string;
  /** Per-record timeout; default 5000. */
  timeoutMs?: number;
  /** Forward the call when recording fails (default true). Otherwise answer the client with a JSON-RPC error. */
  failOpen?: boolean;
  /** Log once per unmapped tool name. */
  warnUnmapped?: boolean;
  /** Override `agent_id` / `workflow_id` from the mapping file. */
  agentId?: string;
  workflowId?: string;
  /** Diagnostics sink; default writes `commitlayer-recorder: <message>` to stderr. */
  log?: (message: string) => void;
  /** Injectable for tests. */
  fetch?: typeof fetch;
  now?: () => string;
}

/** What to do with one client-to-server line: forward it, or answer the client instead. */
export type Intercept = { forward: true } | { forward: false; reply: string };

const FORWARD: Intercept = { forward: true };

interface Message extends Json {
  id?: JsonRpcId;
  method?: string;
  params?: unknown;
}

function parseMessage(line: string): Message | undefined {
  const t = line.trim();
  if (!t.startsWith("{")) return undefined;
  try {
    const v: unknown = JSON.parse(t);
    return isObject(v) ? (v as Message) : undefined;
  } catch {
    return undefined;
  }
}

function idKey(id: JsonRpcId): string {
  return `${typeof id}:${String(id)}`;
}

function messageOf(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

function defaultLog(message: string): void {
  process.stderr.write(`commitlayer-recorder: ${message}\n`);
}

export class Recorder {
  readonly apiUrl: string;
  readonly timeoutMs: number;
  readonly failOpen: boolean;
  readonly warnUnmapped: boolean;
  readonly agentId: string | undefined;
  readonly workflowId: string | undefined;
  /** False when no API key was given: everything is forwarded, nothing is recorded. */
  readonly recording: boolean;

  private readonly apiKey: string;
  private readonly mapping: Mapping;
  private readonly log: (message: string) => void;
  private readonly fetchImpl: typeof fetch;
  private readonly clock: () => string;
  private readonly pending = new Map<string, { actionId: string; tool: string }>();
  private readonly warnedUnmapped = new Set<string>();

  constructor(opts: RecorderOptions) {
    this.mapping = opts.mapping;
    this.apiKey = opts.apiKey ?? "";
    this.apiUrl = (opts.apiUrl || "https://api.commitlayer.ai").replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? 5000;
    this.failOpen = opts.failOpen ?? true;
    this.warnUnmapped = opts.warnUnmapped ?? false;
    this.agentId = opts.agentId ?? opts.mapping.agent_id;
    this.workflowId = opts.workflowId ?? opts.mapping.workflow_id;
    this.log = opts.log ?? defaultLog;
    this.fetchImpl = opts.fetch ?? fetch;
    this.clock = opts.now ?? now;
    this.recording = this.apiKey !== "";
    if (!this.recording) {
      this.log("COMMITLAYER_API_KEY is not set: running as a passthrough, nothing is recorded");
    }
  }

  /**
   * Handle one client-to-server line. A mapped `tools/call` is recorded first; the returned decision says
   * whether to forward the line unchanged or to answer the client with `reply` instead.
   */
  async intercept(line: string): Promise<Intercept> {
    const msg = parseMessage(line);
    if (!msg || msg.method !== "tools/call") return FORWARD;
    const call: Json = isObject(msg.params) ? msg.params : {};
    const tool = typeof call.name === "string" ? call.name : undefined;
    if (tool === undefined) return FORWARD;

    const rule = findTool(this.mapping, tool);
    if (!rule) {
      if (this.warnUnmapped && !this.warnedUnmapped.has(tool)) {
        this.warnedUnmapped.add(tool);
        this.log(`tool ${tool} is not mapped: forwarded without recording`);
      }
      return FORWARD;
    }
    if (!this.recording) return FORWARD;

    const requestId: JsonRpcId = msg.id === undefined ? null : msg.id;
    let body: ActionBody;
    try {
      body = buildAction(rule, call, {
        requestId,
        agentId: this.agentId,
        workflowId: this.workflowId,
        requestedAt: this.clock(),
      });
    } catch (e) {
      if (!(e instanceof PathError)) throw e;
      this.log(`tool ${tool}: ${e.message}: forwarded without recording`);
      return FORWARD;
    }

    try {
      await this.post(body);
    } catch (e) {
      const reason = messageOf(e);
      if (this.failOpen) {
        this.log(`recording failed for ${body.action_id} (${tool}): ${reason}: forwarded anyway`);
        return FORWARD;
      }
      this.log(`recording failed for ${body.action_id} (${tool}): ${reason}: call refused (fail-open is off)`);
      return {
        forward: false,
        reply: JSON.stringify({
          jsonrpc: "2.0",
          id: requestId,
          error: { code: -32000, message: `CommitLayer recording failed: ${reason}` },
        }),
      };
    }
    if (requestId !== null) this.pending.set(idKey(requestId), { actionId: body.action_id, tool });
    return FORWARD;
  }

  /**
   * Observe one server-to-client line. A response to a recorded request logs one line; nothing is sent to
   * the API, the tool's own result is not evidence.
   */
  observe(line: string): void {
    const msg = parseMessage(line);
    if (!msg || msg.id === undefined || msg.id === null) return;
    if (!("result" in msg) && !("error" in msg)) return; // a request from the server, not a response
    const key = idKey(msg.id);
    const rec = this.pending.get(key);
    if (!rec) return;
    this.pending.delete(key);
    const isError = "error" in msg ? true : isObject(msg.result) && msg.result.isError === true;
    this.log(`recorded ${rec.actionId} → tool result isError=${isError}`);
  }

  private async post(body: ActionBody): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(`${this.apiUrl}/v1/actions`, {
        method: "POST",
        headers: {
          "x-api-key": this.apiKey,
          "content-type": "application/json",
          accept: "application/json",
          "user-agent": `commitlayer-mcp-recorder/${VERSION}`,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await res.text().catch(() => "");
      if (res.status >= 400) throw new Error(`HTTP ${res.status}${describeApiError(text)}`);
    } catch (e) {
      if (controller.signal.aborted) throw new Error(`timeout after ${this.timeoutMs}ms`);
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
}

function describeApiError(text: string): string {
  if (!text) return "";
  try {
    const parsed: unknown = JSON.parse(text);
    const err = isObject(parsed) && isObject(parsed.error) ? parsed.error : undefined;
    if (err) {
      const code = typeof err.code === "string" ? err.code : "error";
      const message = typeof err.message === "string" ? err.message : "";
      return ` ${code}${message ? `: ${message}` : ""}`;
    }
  } catch {
    // not JSON
  }
  return ` ${text.slice(0, 200).replace(/\s+/g, " ")}`;
}

// ---------------- the proxy ----------------

export interface RunOptions extends RecorderOptions {
  /** The downstream MCP server command and its arguments. */
  command: string;
  args?: string[];
  /** Default process.stdin / process.stdout / process.stderr. */
  input?: Readable;
  output?: Writable;
  stderr?: Writable;
}

function writeLine(stream: Writable, line: string): Promise<void> {
  return new Promise((resolve) => {
    if (stream.destroyed || !stream.writable) return resolve();
    stream.write(line + "\n", () => resolve());
  });
}

/**
 * Spawn the downstream server and pump lines both ways until the input closes or the child exits.
 * Resolves with the exit code to propagate.
 */
export function run(opts: RunOptions): Promise<number> {
  const input = opts.input ?? process.stdin;
  const output = opts.output ?? process.stdout;
  const errOut = opts.stderr ?? process.stderr;
  const log = opts.log ?? ((message: string) => void errOut.write(`commitlayer-recorder: ${message}\n`));
  const recorder = new Recorder({ ...opts, log });

  return new Promise<number>((resolve) => {
    const child = spawn(opts.command, opts.args ?? [], { stdio: ["pipe", "pipe", "pipe"] });
    const rlIn = createInterface({ input, crlfDelay: Infinity });
    let done = false;
    const signals: NodeJS.Signals[] = ["SIGINT", "SIGTERM", "SIGHUP"];
    const onSignal = (signal: NodeJS.Signals) => child.kill(signal);
    for (const s of signals) process.on(s, onSignal);

    const finish = (code: number) => {
      if (done) return;
      done = true;
      for (const s of signals) process.off(s, onSignal);
      rlIn.close();
      resolve(code);
    };

    child.once("error", (e) => {
      log(`cannot start ${opts.command}: ${e.message}`);
      finish(127);
    });
    child.stdin.on("error", (e: NodeJS.ErrnoException) => {
      if (e.code !== "EPIPE") log(`downstream stdin: ${e.message}`);
    });
    child.stderr.pipe(errOut, { end: false });

    // Client to server: one line at a time, in order; a record completes before its line is forwarded.
    let chain: Promise<void> = Promise.resolve();
    rlIn.on("line", (line) => {
      chain = chain
        .then(async () => {
          const decision = await recorder.intercept(line);
          if (decision.forward) await writeLine(child.stdin, line);
          else await writeLine(output, decision.reply);
        })
        .catch((e: unknown) => log(`internal error: ${messageOf(e)}`));
    });
    rlIn.on("close", () => {
      void chain.then(() => {
        if (!child.stdin.destroyed) child.stdin.end();
      });
    });

    // Server to client: observe, then forward unchanged.
    const rlOut = createInterface({ input: child.stdout, crlfDelay: Infinity });
    rlOut.on("line", (line) => {
      recorder.observe(line);
      output.write(line + "\n");
    });

    child.once("close", (code, signal) => {
      finish(code ?? (signal ? 128 + signalNumber(signal) : 1));
    });
  });
}

function signalNumber(signal: NodeJS.Signals): number {
  const table: Partial<Record<NodeJS.Signals, number>> = { SIGHUP: 1, SIGINT: 2, SIGQUIT: 3, SIGKILL: 9, SIGTERM: 15 };
  return table[signal] ?? 0;
}
