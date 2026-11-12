# CommitLayer MCP Recorder (Python)

A small stdio proxy that sits between an MCP client (Claude Desktop, Claude Code, Cursor, an agent framework)
and any downstream MCP server (a Stripe or Zendesk MCP server, your own). It forwards every JSON-RPC message
unchanged in both directions, and when the client calls a mapped tool it records the intent as a CommitLayer
action **before** forwarding the call. Standard library only; Python 3.10+.

> The agent may declare its intent. It may read the verdict. It must never control the independent facts, the governing contract, or the final evidence.

The recorder only does the first of those three. It never blocks, reorders or alters the agent's traffic, and
the downstream tool's result is never sent to CommitLayer: a tool answering "refund issued" is a claim, not
evidence. Verification reads the systems of record.

```bash
pip install commitlayer-mcp-recorder    # `pip install pyyaml` for .yaml mappings
export COMMITLAYER_API_KEY=spk_...      # a workspace API key; without it the proxy is a passthrough
commitlayer-mcp-recorder --map mapping.json -- npx -y @stripe/mcp --tools=all
```

## Wiring it into a client

Wherever the client is told how to start the server, put the recorder in front and move the real command
after `--`.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stripe": {
      "command": "commitlayer-mcp-recorder",
      "args": ["--map", "/etc/commitlayer/stripe.json", "--agent-id", "support-bot", "--", "npx", "-y", "@stripe/mcp", "--tools=all"],
      "env": {"COMMITLAYER_API_KEY": "spk_..."}
    }
  }
}
```

Claude Code:

```bash
claude mcp add stripe -e COMMITLAYER_API_KEY=spk_... -- commitlayer-mcp-recorder --map stripe.json -- npx -y @stripe/mcp --tools=all
```

Cursor and other clients use the same `command` / `args` / `env` shape. The downstream server does not know
the recorder is there; it sees the same bytes it would have seen.

## CLI

```
commitlayer-mcp-recorder --map mapping.json [--agent-id ID] [--workflow-id ID] [--api-url URL]
    [--timeout 5] [--no-fail-open] [--warn-unmapped] -- <downstream command and args>
```

| Flag | Meaning |
| --- | --- |
| `--map` | Mapping file, JSON or YAML (YAML needs PyYAML). Default `$COMMITLAYER_RECORDER_MAP`. |
| `--agent-id`, `--workflow-id` | Override `agent_id` / `workflow_id` from the mapping file. |
| `--api-url` | Default `$COMMITLAYER_API_URL`, then `https://api.commitlayer.ai`. |
| `--timeout` | Seconds to wait for the API per call (default 5). |
| `--no-fail-open` | When recording fails, refuse the call instead of forwarding it (see below). |
| `--warn-unmapped` | Log each tool that is called but not mapped, once per tool name. |

Environment: `COMMITLAYER_API_KEY` (required to record; without it the recorder warns once at startup and
runs as a pure passthrough), `COMMITLAYER_API_URL`, `COMMITLAYER_RECORDER_MAP`. The key is never logged. All
diagnostics go to stderr, one line each, prefixed `commitlayer-recorder:`. The downstream server's own stderr
is relayed as is.

The proxy exits when the client closes stdin or the downstream server exits, with the server's exit code.

## Mapping file

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
      "params": {"amount": "arguments.amount", "currency": "arguments.currency"},
      "action_id": "refund:{arguments.order_id}",
      "compensates": "arguments.original_action_id"
    }
  }
}
```

- Keys under `tools` are MCP tool names. An exact match wins; a key with a server prefix
  (`stripe.refund_payment`) also matches the bare name (`refund_payment`).
- `contract_id` and `subject` are required per tool. Unknown keys are allowed and ignored.
- `subject`, each `params` value and `compensates` are dotted paths into the `tools/call` params object:
  `arguments.order_id`, `name`, `_meta.progressToken`, `arguments.items.0`.
- `action_id` is a template with `{path}` placeholders. Without it the id is `mcp:{tool}:{request id}`, so a
  retried request updates one record instead of creating two.
- `subject` may also be a template (`zendesk_ticket_{arguments.ticket_id}`) when the system of record's subject
  carries a prefix the tool call does not; a plain path is used as is (since 0.1.2).
- `contract_version` (integer), `action_type` and `subject_kind` are passed through.
- A `params` path that is absent from the call is skipped; an absent `compensates` path is omitted. An absent
  `subject` (or `action_id` placeholder) means the call is forwarded **but not recorded**, with a warning.

The mapping is validated on load; an invalid file stops the proxy with exit code 2 and a message naming the
tool and key.

### Example: the Coinbase MCP server

[`examples/coinbase.json`](examples/coinbase.json) maps `orders_create` (the order tool of the Coinbase MCP
server behind Coinbase for Agents) to the `coinbase_agent_trade` library contract: the subject is the
product id the agent passes, `amount` is its quote size, `side` and `client_order_id` ride along, and the
action id is derived from the client order id so a retried call updates one record. The paths follow the
create-order request; run the recorder once with `--warn-unmapped` against the server's own `tools/list`
and adjust them if the argument names differ. The Coinbase server is remote (HTTP), and the recorder is a
stdio proxy: put it in front of whatever stdio bridge the client uses to reach the remote server, so the
bridge is the "downstream command". Verification then reads the order and its fills from Coinbase through
the Coinbase connector; the tool's own answer is never evidence.

## What is recorded

For each client → server `tools/call` whose `params.name` is mapped, the recorder posts one action to
`POST {api_url}/v1/actions` (`x-api-key`, `content-type: application/json`) and then forwards the original
line unchanged:

```json
{
  "action_id": "refund:order_10231",
  "contract_id": "refund_workflow",
  "subject_id": "order_10231",
  "subject_kind": "order",
  "action_type": "refund_order",
  "agent_id": "support-bot",
  "workflow_id": "refunds",
  "requested_at": "2026-10-15T12:00:00Z",
  "params": {"amount": 180.0, "currency": "USD", "mcp_tool": "refund_payment", "mcp_request_id": "7"},
  "channel": "mcp"
}
```

`params` always carries `mcp_tool` and `mcp_request_id`. `requested_at` is taken when the call is seen, so
it precedes every effect. Unmapped tools, notifications, `initialize`, `tools/list` and everything else pass
straight through.

When the downstream answer for a recorded request comes back, the recorder logs one line:

```
commitlayer-recorder: recorded refund:order_10231 → tool result isError=false
```

That is all it does with the result. **The tool's own "success" is never evidence.** It is not sent to the
API and it does not change the verdict; CommitLayer settles the action from the facts the systems of record
report, under the governing contract. Read the verdict with the SDK or `GET /v1/actions/{action_id}`.

## Fail-open, and the alternative

By default recording is fail-open: if the API answers 4xx/5xx, times out or cannot be reached, a warning is
written to stderr and the call is forwarded anyway. A verifier that is down must not stop the business
action; the gap shows up as an action that was never recorded rather than one that never happened.

With `--no-fail-open` a call whose recording fails is **not** forwarded. The client receives a JSON-RPC
error for that request id (`code -32000`, message `CommitLayer recording failed: …`) and the downstream
server never sees it. Other traffic is unaffected.

## Library use

```python
from commitlayer_mcp_recorder import Recorder, build_action, load_mapping

mapping = load_mapping("mapping.json")
recorder = Recorder(mapping, api_key="spk_...", agent_id="support-bot")
decision = recorder.before_forward(message)  # a parsed client message; .forward, .action_id, .error
recorder.after_response(message)  # a parsed server message; logs the isError flag
```

`build_action(tool_name, call_params, tool_mapping, request_id=...)` returns the body shown above.

## Limits

- Stdio transport only, newline-delimited JSON-RPC. Lines that are not JSON are forwarded untouched.
- JSON-RPC batch arrays are forwarded untouched and not inspected.
- A `tools/call` without a JSON-RPC id is forwarded without recording (there is nothing to match the answer
  to).

## Tests

```bash
python -m pytest recorder/python/tests -q
```
