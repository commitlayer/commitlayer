# @commitlayer/mcp-recorder

A small stdio proxy that sits between an MCP client (Claude Desktop, Claude Code, Cursor, an agent framework)
and any downstream MCP server (a Stripe server, a Zendesk server, your own). It forwards every JSON-RPC message
unchanged in both directions and, when the client calls a mapped tool, records the intent as a CommitLayer
action **before** forwarding the call. Zero runtime dependencies; Node 20+.

The agent may declare its intent. It may read the verdict. It must never control the independent facts, the
governing contract, or the final evidence.

```
MCP client  ──stdin/stdout──▶  commitlayer-mcp-recorder  ──stdin/stdout──▶  downstream MCP server
                                        │
                                        └── POST /v1/actions  (mapped tools/call only, before forwarding)
```

What the recorder does **not** do: it never blocks, reorders or alters the agent's traffic; it never sends the
downstream result to the API; it never treats the tool's own "success" as evidence. CommitLayer verifies from the
systems of record (payments, CRM, ticketing), not from what the tool said. A tool result with `isError: false`
is a claim by the agent's side of the line; the verdict comes from the facts.

## Install

```bash
npm install -g @commitlayer/mcp-recorder
export COMMITLAYER_API_KEY=spk_...            # a workspace API key; never written to logs
export COMMITLAYER_API_URL=https://api.commitlayer.ai   # the default
```

## Wrap a server

Put the recorder in front of the downstream command, after `--`:

```bash
commitlayer-mcp-recorder --map mapping.json --agent-id support-bot -- npx -y @stripe/mcp --tools=all
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stripe": {
      "command": "commitlayer-mcp-recorder",
      "args": ["--map", "/etc/commitlayer/stripe.json", "--", "npx", "-y", "@stripe/mcp", "--tools=all"],
      "env": { "COMMITLAYER_API_KEY": "spk_..." }
    }
  }
}
```

Claude Code:

```bash
claude mcp add stripe -e COMMITLAYER_API_KEY=spk_... -- commitlayer-mcp-recorder --map ./stripe.json -- npx -y @stripe/mcp --tools=all
```

Cursor and other clients take the same `command` / `args` / `env` shape. The client sees exactly the tools the
downstream server offers; nothing is added or hidden.

## The mapping file

Only the tools listed in the mapping are recorded; every other call is forwarded without a record.

```json
{
  "agent_id": "support-bot",
  "workflow_id": "refunds",
  "tools": {
    "refund_payment": {
      "contract_id": "refund_workflow",
      "action_type": "refund_order",
      "subject": "arguments.order_id",
      "subject_kind": "order",
      "params": { "amount": "arguments.amount", "currency": "arguments.currency" },
      "action_id": "refund:{arguments.order_id}",
      "compensates": "arguments.original_action_id"
    }
  }
}
```

| key                | required | meaning                                                                                                    |
| ------------------ | -------- | ---------------------------------------------------------------------------------------------------------- |
| `contract_id`      | yes      | the contract that governs this action                                                                      |
| `subject`          | yes      | path to the subject id (an order, a ticket). A call where it is missing is forwarded and **not** recorded   |
| `contract_version` | no       | pin a contract version                                                                                     |
| `action_type`      | no       | e.g. `refund_order`                                                                                        |
| `subject_kind`     | no       | e.g. `order`                                                                                               |
| `params`           | no       | parameter name to path; the values the contract limits. Missing values are left out                        |
| `action_id`        | no       | template with `{path}` placeholders. Default `mcp:{tool}:{request id}`                                     |
| `compensates`      | no       | path to the id of the earlier action this call corrects; sent only when present                            |

Paths are dotted paths into the `tools/call` params object: `arguments.order_id`, `name`, `_meta.progressToken`,
`arguments.items.0.id`. A mapping key may carry a server prefix, `stripe.refund_payment`, and still matches a call
to `refund_payment`. `agent_id` and `workflow_id` at the top level apply to every tool; `--agent-id` and
`--workflow-id` on the command line override them. The file is validated on load and a bad file stops the
recorder with a message naming the tool and the key (exit code 2), before any traffic flows.

Two parameters are always added: `mcp_tool` (the tool name) and `mcp_request_id` (the JSON-RPC id as a string).
With the default `action_id` a retried request (same id) updates the one record instead of creating a second.
Give your own template when the subject carries the identity, as in `refund:{arguments.order_id}`.

See [`examples/mapping.json`](examples/mapping.json). [`examples/coinbase.json`](examples/coinbase.json) maps
`orders_create` of the Coinbase MCP server (Coinbase for Agents) to the `coinbase_agent_trade` library
contract: the product id is the subject, the quote size is `amount`, the action id comes from the client order
id. The paths follow the create-order request; run once with `--warn-unmapped` against the server's own tool
list and adjust them if the argument names differ. That server is remote and the recorder is a stdio proxy,
so it goes in front of the stdio bridge the client uses to reach it.

## What gets recorded, and when

For a client-to-server `tools/call` whose `params.name` is mapped, the recorder builds this body and sends
`POST {api url}/v1/actions` with the `x-api-key` header, then forwards the original line unchanged:

```json
{
  "action_id": "refund:ord_10231",
  "contract_id": "refund_workflow",
  "action_type": "refund_order",
  "subject_id": "ord_10231",
  "subject_kind": "order",
  "agent_id": "support-bot",
  "workflow_id": "refunds",
  "requested_at": "2026-10-13T10:42:09Z",
  "params": { "amount": 180, "currency": "EUR", "mcp_tool": "refund_payment", "mcp_request_id": "7" },
  "channel": "mcp"
}
```

The record completes (or fails) before the line is forwarded, so the request time precedes every effect and the
order of the agent's messages is preserved; later lines queue behind it. `requested_at` is the current UTC time.

When the downstream server answers, the recorder matches the response by JSON-RPC id and writes one line to
stderr:

```
commitlayer-recorder: recorded refund:ord_10231 → tool result isError=false
```

That is all it does with the result. **The tool's own success is never evidence.** Nothing from the response is
sent to the API; CommitLayer settles the action from the systems of record, and a verdict that is not yet a PASS
stays pending until the window closes and every source has caught up.

## Failure handling

- **Fail-open (default).** If the API returns 4xx/5xx, times out (`--timeout`, default 5 seconds) or cannot be
  reached, the call is forwarded anyway and a warning goes to stderr. The gap shows up as an action that was
  never recorded rather than one that never happened.
- **`--no-fail-open`.** The call is not forwarded; the client receives a JSON-RPC error for that request id,
  `code -32000`, message `CommitLayer recording failed: …`. Later traffic flows normally.
- **No `COMMITLAYER_API_KEY`.** The recorder warns once at start and runs as a pure passthrough.
- **Unmapped tool.** Forwarded, not recorded. `--warn-unmapped` logs once per tool name.
- **Missing subject.** Forwarded, not recorded, with a warning naming the path.

Lines that are not JSON are forwarded untouched. The downstream server's stderr is passed through. The recorder
exits when its stdin closes or the child exits, with the child's exit code, and forwards SIGINT/SIGTERM/SIGHUP
to the child. All diagnostics go to stderr, prefixed `commitlayer-recorder:`; the API key is never logged.

## Command line

```
commitlayer-mcp-recorder --map mapping.json [options] -- <downstream command> [args...]

  --map <file>          mapping file (default: $COMMITLAYER_RECORDER_MAP)
  --agent-id <id>       overrides agent_id from the mapping file
  --workflow-id <id>    overrides workflow_id from the mapping file
  --api-url <url>       default $COMMITLAYER_API_URL or https://api.commitlayer.ai
  --timeout <seconds>   per-record timeout (default 5)
  --no-fail-open        answer the client with a JSON-RPC error instead of forwarding when recording fails
  --warn-unmapped       log once per tool name that is called but not mapped
  --version, --help
```

## As a library

```ts
import { loadMapping, buildAction, Recorder, run } from "@commitlayer/mcp-recorder";

const mapping = loadMapping("mapping.json");

// The body that would be posted for one tools/call (throws PathError when the subject is missing).
const body = buildAction(mapping.tools.refund_payment, { name: "refund_payment", arguments: { order_id: "o1" } }, { requestId: 7 });

// Decide line by line: intercept() records a mapped call and says whether to forward it or answer the client.
const rec = new Recorder({ mapping, apiKey: process.env.COMMITLAYER_API_KEY });
const decision = await rec.intercept(line); // { forward: true } | { forward: false, reply }
rec.observe(responseLine); // logs "recorded <action_id> → tool result isError=<bool>"

// Or run the whole proxy around a command; resolves with the exit code to propagate.
const code = await run({ mapping, command: "npx", args: ["-y", "@stripe/mcp"], apiKey: process.env.COMMITLAYER_API_KEY });
```

`fetch`, `now` and `log` are injectable for tests.

## Limits

- JSON-RPC batches (arrays) are forwarded unchanged and not inspected; MCP's stdio transport sends one message
  per line.
- `notifications/cancelled` does not undo a record: the intent was declared; the facts decide what happened.
- Only the stdio transport is covered. For HTTP transports, record from the agent with the SDK instead.

## Develop

```bash
cd recorder/js
npm install   # typescript and @types/node only
npm run build # compiles src/ to dist/
npm test      # compiles the node:test suite and runs it offline against a fake MCP server and a fake API
```
