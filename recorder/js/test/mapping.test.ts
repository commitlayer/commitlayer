import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  buildAction,
  findTool,
  loadMapping,
  MappingError,
  parseMapping,
  PathError,
  renderTemplate,
  resolvePath,
  VERSION,
  type ToolRule,
} from "../src/index.js";

const dir = mkdtempSync(join(tmpdir(), "commitlayer-recorder-"));

function mappingFile(name: string, content: unknown): string {
  const file = join(dir, name);
  writeFileSync(file, typeof content === "string" ? content : JSON.stringify(content));
  return file;
}

const refundRule: ToolRule = {
  contract_id: "refund_workflow",
  action_type: "refund_order",
  subject: "arguments.order_id",
  subject_kind: "order",
  params: { amount: "arguments.amount", currency: "arguments.currency" },
  action_id: "refund:{arguments.order_id}",
  compensates: "arguments.original_action_id",
};

test("version", () => {
  assert.equal(VERSION, "0.1.0");
});

// ---------------- loadMapping / parseMapping ----------------

test("loadMapping: a valid file, including a prefixed tool name", () => {
  const file = mappingFile("ok.json", {
    agent_id: "support-bot",
    workflow_id: "refunds",
    tools: { refund_payment: refundRule, "stripe.charge_customer": { contract_id: "c", subject: "arguments.id" } },
  });
  const m = loadMapping(file);
  assert.equal(m.agent_id, "support-bot");
  assert.equal(m.workflow_id, "refunds");
  assert.deepEqual(Object.keys(m.tools), ["refund_payment", "stripe.charge_customer"]);
  assert.deepEqual(m.tools.refund_payment, refundRule);
  assert.deepEqual(m.tools["stripe.charge_customer"], { contract_id: "c", subject: "arguments.id" });
});

test("loadMapping: the example mapping shipped with the package is valid", () => {
  const m = loadMapping(new URL("../../examples/mapping.json", import.meta.url).pathname);
  assert.equal(m.tools.refund_payment.contract_id, "refund_workflow");
});

test("loadMapping: missing file and invalid JSON", () => {
  assert.throws(() => loadMapping(join(dir, "nope.json")), (e: unknown) => {
    assert.ok(e instanceof MappingError);
    assert.match(e.message, /cannot read mapping file .*nope\.json/);
    return true;
  });
  assert.throws(() => loadMapping(mappingFile("bad.json", "{not json")), /bad\.json: not valid JSON/);
});

test("parseMapping: validation errors name the tool and the key", () => {
  const cases: [unknown, RegExp][] = [
    [[], /must be a JSON object/],
    [{}, /tools must be an object/],
    [{ tools: [] }, /tools must be an object/],
    [{ tools: {}, extra: 1 }, /extra is not a known key/],
    [{ agent_id: 7, tools: {} }, /agent_id must be a string/],
    [{ tools: { t: "x" } }, /tools\.t must be an object/],
    [{ tools: { t: { subject: "arguments.id" } } }, /tools\.t\.contract_id is required/],
    [{ tools: { t: { contract_id: "" , subject: "s" } } }, /tools\.t\.contract_id is required/],
    [{ tools: { t: { contract_id: "c" } } }, /tools\.t\.subject is required/],
    [{ tools: { t: { contract_id: "c", subject: 3 } } }, /tools\.t\.subject must be a string/],
    [{ tools: { t: { contract_id: "c", subject: "s", typo: 1 } } }, /tools\.t\.typo is not a known key/],
    [{ tools: { t: { contract_id: "c", subject: "s", contract_version: "1" } } }, /contract_version must be an integer/],
    [{ tools: { t: { contract_id: "c", subject: "s", params: ["a"] } } }, /params must be an object of name to path/],
    [{ tools: { t: { contract_id: "c", subject: "s", params: { amount: 5 } } } }, /params\.amount must be a path string/],
    [{ tools: { t: { contract_id: "c", subject: "s", action_id: 5 } } }, /action_id must be a string/],
  ];
  for (const [raw, re] of cases) {
    assert.throws(() => parseMapping(raw, "m.json"), (e: unknown) => {
      assert.ok(e instanceof MappingError, `expected MappingError for ${JSON.stringify(raw)}`);
      assert.match(e.message, re);
      assert.match(e.message, /^m\.json/);
      return true;
    });
  }
});

test("parseMapping: an empty tools object is allowed (pure passthrough)", () => {
  assert.deepEqual(parseMapping({ tools: {} }), { tools: {} });
});

// ---------------- findTool ----------------

test("findTool: exact match, server prefix, no match, no prototype leakage", () => {
  const m = parseMapping({
    tools: {
      refund_payment: { contract_id: "a", subject: "s" },
      "stripe.charge_customer": { contract_id: "b", subject: "s" },
      "zendesk.tickets.close_ticket": { contract_id: "c", subject: "s" },
    },
  });
  assert.equal(findTool(m, "refund_payment")?.contract_id, "a");
  assert.equal(findTool(m, "charge_customer")?.contract_id, "b");
  assert.equal(findTool(m, "stripe.charge_customer")?.contract_id, "b");
  assert.equal(findTool(m, "close_ticket")?.contract_id, "c");
  assert.equal(findTool(m, "tickets.close_ticket")?.contract_id, "c");
  assert.equal(findTool(m, "customer"), undefined); // a prefix match needs the whole segment
  assert.equal(findTool(m, "constructor"), undefined);
  assert.equal(findTool(m, "unknown"), undefined);
});

// ---------------- resolvePath / renderTemplate ----------------

test("resolvePath: nested objects, array indexes, missing segments", () => {
  const call = { name: "t", arguments: { order_id: "o1", items: [{ id: "i0" }], n: 0, f: false }, _meta: { progressToken: 9 } };
  assert.equal(resolvePath(call, "name"), "t");
  assert.equal(resolvePath(call, "arguments.order_id"), "o1");
  assert.equal(resolvePath(call, "arguments.items.0.id"), "i0");
  assert.equal(resolvePath(call, "_meta.progressToken"), 9);
  assert.equal(resolvePath(call, "arguments.n"), 0);
  assert.equal(resolvePath(call, "arguments.f"), false);
  assert.equal(resolvePath(call, "arguments.missing"), undefined);
  assert.equal(resolvePath(call, "arguments.order_id.deeper"), undefined);
  assert.equal(resolvePath(call, "arguments.items.5.id"), undefined);
  assert.equal(resolvePath(null, "a"), undefined);
  assert.equal(resolvePath({}, "constructor"), undefined);
});

test("renderTemplate: placeholders, literals, and a missing path", () => {
  const call = { name: "refund_payment", arguments: { order_id: "o1", n: 42 } };
  assert.equal(renderTemplate("refund:{arguments.order_id}", call), "refund:o1");
  assert.equal(renderTemplate("{name}:{arguments.order_id}:{ arguments.n }", call), "refund_payment:o1:42");
  assert.equal(renderTemplate("static", call), "static");
  assert.throws(() => renderTemplate("x:{arguments.nope}", call), (e: unknown) => {
    assert.ok(e instanceof PathError);
    assert.match(e.message, /template path "arguments.nope" is missing/);
    return true;
  });
  assert.throws(() => renderTemplate("x:{arguments}", call), /must be a string, number or boolean, got object/);
});

// ---------------- buildAction ----------------

const refundCall = {
  name: "refund_payment",
  arguments: { order_id: "ord_1", amount: 180, currency: "EUR", original_action_id: "charge:ord_1", note: "x" },
};

test("buildAction: full body from the refund rule", () => {
  const body = buildAction(refundRule, refundCall, {
    requestId: 7,
    agentId: "support-bot",
    workflowId: "refunds",
    requestedAt: "2026-10-13T10:00:00Z",
  });
  assert.deepEqual(body, {
    action_id: "refund:ord_1",
    contract_id: "refund_workflow",
    subject_id: "ord_1",
    requested_at: "2026-10-13T10:00:00Z",
    params: { amount: 180, currency: "EUR", mcp_tool: "refund_payment", mcp_request_id: "7" },
    channel: "mcp",
    workflow_id: "refunds",
    agent_id: "support-bot",
    action_type: "refund_order",
    subject_kind: "order",
    compensates: "charge:ord_1",
  });
});

test("buildAction: defaults, string ids, numeric subject, optional keys absent", () => {
  const rule: ToolRule = { contract_id: "c", subject: "arguments.id", contract_version: 3 };
  const body = buildAction(rule, { name: "t", arguments: { id: 42 } }, { requestId: "req-9" });
  assert.equal(body.action_id, "mcp:t:req-9");
  assert.equal(body.subject_id, "42");
  assert.equal(body.contract_version, 3);
  assert.deepEqual(body.params, { mcp_tool: "t", mcp_request_id: "req-9" });
  assert.match(body.requested_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  for (const k of ["workflow_id", "agent_id", "action_type", "subject_kind", "compensates"]) {
    assert.ok(!(k in body), `${k} must be absent`);
  }
});

test("buildAction: a subject template spells out the system of record's prefix", () => {
  const rule: ToolRule = {
    contract_id: "scop_resolve_customer_case",
    subject: "zendesk_ticket_{arguments.ticket_id}",
    action_id: "resolve:zendesk_ticket_{arguments.ticket_id}",
  };
  const body = buildAction(rule, { name: "update_ticket", arguments: { ticket_id: 4411 } }, { requestId: 1 });
  assert.equal(body.subject_id, "zendesk_ticket_4411");
  assert.equal(body.action_id, "resolve:zendesk_ticket_4411");
  assert.throws(
    () => buildAction(rule, { name: "update_ticket", arguments: {} }, { requestId: 2 }),
    (e: unknown) => e instanceof PathError,
  );
});

test("buildAction: the same request id gives the same action id (retries update one record)", () => {
  const rule: ToolRule = { contract_id: "c", subject: "arguments.id" };
  const a = buildAction(rule, { name: "t", arguments: { id: "x" } }, { requestId: 1 });
  const b = buildAction(rule, { name: "t", arguments: { id: "x" } }, { requestId: 1 });
  assert.equal(a.action_id, b.action_id);
  assert.equal(a.action_id, "mcp:t:1");
});

test("buildAction: no request id gets a unique action id", () => {
  const rule: ToolRule = { contract_id: "c", subject: "arguments.id" };
  const a = buildAction(rule, { name: "t", arguments: { id: "x" } });
  const b = buildAction(rule, { name: "t", arguments: { id: "x" } }, { requestId: null });
  assert.match(a.action_id, /^mcp:t:[0-9a-f-]{36}$/);
  assert.notEqual(a.action_id, b.action_id);
});

test("buildAction: missing params and compensates are left out; missing subject throws PathError", () => {
  const body = buildAction(refundRule, { name: "refund_payment", arguments: { order_id: "o2" } }, { requestId: 1 });
  assert.deepEqual(body.params, { mcp_tool: "refund_payment", mcp_request_id: "1" });
  assert.ok(!("compensates" in body));

  assert.throws(() => buildAction(refundRule, { name: "refund_payment", arguments: {} }, { requestId: 1 }), (e: unknown) => {
    assert.ok(e instanceof PathError);
    assert.match(e.message, /subject path "arguments.order_id" is missing/);
    return true;
  });
  assert.throws(() => buildAction(refundRule, { name: "refund_payment" }, { requestId: 1 }), PathError);
  assert.throws(
    () => buildAction(refundRule, { name: "refund_payment", arguments: { order_id: { nested: 1 } } }, { requestId: 1 }),
    /subject path "arguments.order_id" must be a string, number or boolean/,
  );
});
