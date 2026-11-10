# CommitLayer for LangGraph and LangChain agents

Record a tool call as a CommitLayer action before the tool runs, then gate the graph on the verdict.

```bash
pip install 'commitlayer-integrations[langgraph]'
export COMMITLAYER_API_KEY=spk_...     # a recorder key: records actions, reads verdicts, nothing else
```

## Record every mapped tool call

```python
from commitlayer import Client
from commitlayer_integrations.tools import ToolRecorder
from commitlayer_langgraph import record_tool_calls
from langgraph.prebuilt import ToolNode

MAPPING = {
    "refund_payment": {
        "contract_id": "refund_workflow",
        "action_type": "refund_order",
        "subject": "arguments.order_id",
        "subject_kind": "order",
        "params": {"amount": "arguments.amount", "currency": "arguments.currency"},
        "action_id": "refund:{arguments.order_id}",
    }
}
recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)

tools = ToolNode([refund_payment, close_ticket], wrap_tool_call=record_tool_calls(recorder))
```

With LangChain v1 agents the same function is middleware:

```python
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call

agent = create_agent(
    model, tools=[refund_payment, close_ticket], middleware=[wrap_tool_call(record_tool_calls(recorder))]
)
```

The wrapper reads the tool's name, arguments and call id from the `ToolCallRequest`, posts the mapped ones to
`POST /v1/actions`, and hands the request to the framework's handler unchanged. The tool's result is never
sent. Recording is fail-open: if the API cannot be reached the tool still runs and a warning is logged.

## Gate the graph on the verdict

```python
from commitlayer_langgraph import verdict_gate_node

graph.add_node("verify", verdict_gate_node(recorder, timeout=300))
graph.add_conditional_edges(
    "verify",
    lambda s: s["commitlayer_verdict"]["policy"],
    {"PASS": "notify_customer", "FAIL": "recover", "UNKNOWN": "recover"},
)
```

The node waits for the last recorded action (or the one named in `state["commitlayer_action_id"]`) to settle
and writes `{status, outcome, policy, headline, verification}` into the state. A timeout is `status: "pending"`
and is the graph's decision, never a success.

The mapping format, paths and templates are documented in `commitlayer_integrations.tools`.
