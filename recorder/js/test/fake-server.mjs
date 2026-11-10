// A minimal downstream MCP server over stdio for the recorder's tests: newline-delimited JSON-RPC 2.0.
// It answers initialize, tools/list and tools/call (echoing the arguments; isError when arguments.fail is set),
// plus two test-only methods: test/calls (how many tools/call it has seen) and test/exit (exit with a code).
// Lines that are not JSON are echoed back unchanged so the test can see they were forwarded.
import { createInterface } from "node:readline";

const tools = [
  {
    name: "refund_payment",
    description: "Refund a payment",
    inputSchema: { type: "object", properties: { order_id: { type: "string" }, amount: { type: "number" } } },
  },
  {
    name: "lookup_order",
    description: "Look up an order",
    inputSchema: { type: "object", properties: { order_id: { type: "string" } } },
  },
];

let calls = 0;

function reply(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    process.stdout.write(line + "\n");
    return;
  }
  if (msg === null || typeof msg !== "object" || msg.id === undefined) return; // a notification
  const params = msg.params ?? {};
  switch (msg.method) {
    case "initialize":
      reply(msg.id, {
        protocolVersion: "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "fake-server", version: "0.0.0" },
      });
      break;
    case "tools/list":
      reply(msg.id, { tools });
      break;
    case "tools/call": {
      calls += 1;
      const args = params.arguments ?? {};
      reply(msg.id, {
        content: [{ type: "text", text: JSON.stringify({ tool: params.name, arguments: args }) }],
        isError: Boolean(args.fail),
      });
      break;
    }
    case "test/calls":
      reply(msg.id, { calls });
      break;
    case "test/exit":
      process.exit(params.code ?? 0);
      break;
    default:
      process.stdout.write(
        JSON.stringify({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: `unknown method ${msg.method}` } }) +
          "\n",
      );
  }
});
rl.on("close", () => process.exit(0));
