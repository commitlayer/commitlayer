#!/usr/bin/env node
/**
 * commitlayer-mcp-recorder: the bin entry. Parses the flags before `--`, loads the mapping, and runs the proxy
 * around the downstream command after `--`. Every diagnostic goes to stderr, prefixed `commitlayer-recorder:`.
 * The API key is read from COMMITLAYER_API_KEY and never logged.
 */
import { loadMapping, MappingError, run, VERSION } from "./index.js";

const USAGE = `usage: commitlayer-mcp-recorder --map mapping.json [options] -- <downstream command> [args...]

Sits between an MCP client and a downstream MCP server over stdio. Forwards every message unchanged and records
each mapped tools/call as a CommitLayer action before forwarding it.

options:
  --map <file>          mapping file (default: $COMMITLAYER_RECORDER_MAP)
  --agent-id <id>       overrides agent_id from the mapping file
  --workflow-id <id>    overrides workflow_id from the mapping file
  --api-url <url>       default $COMMITLAYER_API_URL or https://api.commitlayer.ai
  --timeout <seconds>   time allowed for one record, retries included (default 5)
  --retries <n>         times a record refused with 429 or 503 and Retry-After is sent again (default 2; 0 is off)
  --no-fail-open        answer the client with a JSON-RPC error instead of forwarding when recording fails
  --warn-unmapped       log once per tool name that is called but not mapped
  --version, --help

environment:
  COMMITLAYER_API_KEY        required to record; without it the recorder warns once and is a pure passthrough
  COMMITLAYER_API_URL        default API base URL
  COMMITLAYER_RECORDER_MAP   default for --map
`;

function fail(message: string): never {
  process.stderr.write(`commitlayer-recorder: ${message}\n`);
  process.exit(2);
}

interface Parsed {
  map?: string;
  agentId?: string;
  workflowId?: string;
  apiUrl?: string;
  timeout?: number;
  retries?: number;
  failOpen: boolean;
  warnUnmapped: boolean;
  command: string[];
}

function parseArgs(argv: string[]): Parsed {
  const out: Parsed = { failOpen: true, warnUnmapped: false, command: [] };
  let i = 0;
  const value = (flag: string, inline: string | undefined): string => {
    if (inline !== undefined) return inline;
    const v = argv[i + 1];
    if (v === undefined || v === "--") fail(`${flag} needs a value`);
    i += 1;
    return v;
  };
  for (; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--") {
      out.command = argv.slice(i + 1);
      return out;
    }
    const eq = arg.indexOf("=");
    const flag = eq === -1 ? arg : arg.slice(0, eq);
    const inline = eq === -1 ? undefined : arg.slice(eq + 1);
    switch (flag) {
      case "--map":
        out.map = value(flag, inline);
        break;
      case "--agent-id":
        out.agentId = value(flag, inline);
        break;
      case "--workflow-id":
        out.workflowId = value(flag, inline);
        break;
      case "--api-url":
        out.apiUrl = value(flag, inline);
        break;
      case "--timeout": {
        const n = Number(value(flag, inline));
        if (!Number.isFinite(n) || n <= 0) fail("--timeout must be a positive number of seconds");
        out.timeout = n;
        break;
      }
      case "--retries": {
        const n = Number(value(flag, inline));
        if (!Number.isInteger(n) || n < 0) fail("--retries must be a whole number, 0 or more");
        out.retries = n;
        break;
      }
      case "--no-fail-open":
        out.failOpen = false;
        break;
      case "--warn-unmapped":
        out.warnUnmapped = true;
        break;
      case "--help":
      case "-h":
        process.stdout.write(USAGE);
        return process.exit(0);
      case "--version":
      case "-V":
        process.stdout.write(`${VERSION}\n`);
        return process.exit(0);
      default:
        fail(`unknown option ${arg}\n\n${USAGE}`);
    }
  }
  fail(`missing "--" before the downstream command\n\n${USAGE}`);
}

const parsed = parseArgs(process.argv.slice(2));
const mapFile = parsed.map ?? process.env.COMMITLAYER_RECORDER_MAP;
if (!mapFile) fail(`--map is required (or set COMMITLAYER_RECORDER_MAP)\n\n${USAGE}`);
if (parsed.command.length === 0) fail(`no downstream command after "--"\n\n${USAGE}`);

let mapping;
try {
  mapping = loadMapping(mapFile);
} catch (e) {
  fail(e instanceof MappingError ? e.message : String(e));
}

const [command, ...args] = parsed.command;
const code = await run({
  mapping,
  command,
  args,
  apiKey: process.env.COMMITLAYER_API_KEY,
  apiUrl: parsed.apiUrl ?? process.env.COMMITLAYER_API_URL,
  timeoutMs: parsed.timeout === undefined ? undefined : Math.round(parsed.timeout * 1000),
  maxRetries: parsed.retries,
  failOpen: parsed.failOpen,
  warnUnmapped: parsed.warnUnmapped,
  agentId: parsed.agentId,
  workflowId: parsed.workflowId,
});
process.exitCode = code;
