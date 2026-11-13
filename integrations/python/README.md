# commitlayer-integrations (Python)

CommitLayer for agent frameworks and durable-workflow runtimes. One distribution, six optional packages, built on the
[`commitlayer` SDK](../../sdk/python/README.md):

| Package | Framework or runtime | Install |
|---|---|---|
| [`commitlayer_langgraph`](commitlayer_langgraph/README.md) | LangGraph, LangChain v1 agents | `pip install 'commitlayer-integrations[langgraph]'` |
| [`commitlayer_crewai`](commitlayer_crewai/README.md) | CrewAI | `pip install 'commitlayer-integrations[crewai]'` |
| [`commitlayer_openai_agents`](commitlayer_openai_agents/README.md) | OpenAI Agents SDK | `pip install 'commitlayer-integrations[openai-agents]'` |
| [`commitlayer_pydantic_ai`](commitlayer_pydantic_ai/README.md) | Pydantic AI | `pip install 'commitlayer-integrations[pydantic-ai]'` |
| [`commitlayer_temporal`](commitlayer_temporal/README.md) | Temporal (`temporalio`) | `pip install 'commitlayer-integrations[temporal]'` |
| [`commitlayer_inngest`](commitlayer_inngest/README.md) | Inngest (`inngest`) | `pip install 'commitlayer-integrations[inngest]'` |
| [`commitlayer_x402`](commitlayer_x402/README.md) | x402 payments on `httpx` (any client through two methods) | `pip install 'commitlayer-integrations[x402]'` |

The four framework packages share one mapping and one recorder (`commitlayer_integrations.tools.ToolRecorder`):
a tool name maps to a contract, the argument that holds the subject, and the arguments that are the action's
parameters, in the same shape the MCP recorder's mapping file uses. Each framework package is the hook at
which that recorder runs: a `wrap_tool_call` wrapper for LangGraph, a `before_tool_call` hook for CrewAI,
`RunHooks` for the OpenAI Agents SDK, a `WrapperToolset` for Pydantic AI. None of them imports its framework
at module load, so the tests run without any of them installed.

Each one does the same two things in its runtime's own terms:

1. **Record the agent's intent before the work runs** (an activity, a step) as an action under a contract:
   `POST /v1/actions` with the contract, subject and parameters taken from the call's own arguments. The
   result of the work is never sent.
2. **Gate completion on the verdict**: wait for `GET /v1/actions/{ref}` to report a *settled* verification and
   branch on PASS / FAIL / UNKNOWN. The workflow does not tell the customer "done" until the verdict is PASS.

The workflow engine makes the process durable. CommitLayer independently verifies the result, from the
systems of record, after the fact. The agent may declare its intent. It may read the verdict. It must never
control the independent facts, the governing contract, or the final evidence.

## When the API is busy

Every integration records through the `commitlayer` client, so the client's rule applies to all of them (SDK
0.1.3 and later; see [What is retried](../../sdk/python/README.md#what-is-retried)): an answer of `429` or `503`
that carries `Retry-After` is waited out and the same body, with the same `action_id`, is sent again, three
times at most and only for a stated wait of ten seconds or less. No other answer is retried. An integration
never adds a retry of its own, and its fail-open policy applies to the last answer.

The wait is a blocking sleep in the client, so where it happens matters:

| Package | Where the request and the wait run |
|---|---|
| `commitlayer_openai_agents`, `commitlayer_pydantic_ai` | In a worker thread (`ToolRecorder.record_async`, since 0.1.3), so the event loop keeps serving other tasks. The tool still starts only after the call is recorded. |
| `commitlayer_temporal` | The interceptor records in the loop's executor and the verifier is a synchronous activity on the worker's `activity_executor`: never on the worker's event loop. The wait counts against the activity's `start_to_close_timeout`. |
| `commitlayer_inngest` | Inside the step: in the calling thread for a sync function, in the loop's executor for an async one. |
| `commitlayer_langgraph` | A graph run with `invoke` or `stream` calls `record_tool_calls` in the thread that runs the node. A graph run with `ainvoke` or `astream` calls the wrapper on its event loop: give the node `awrap_tool_call=record_tool_calls_async(recorder)` as well (since 0.1.3), which records in a worker thread. With the synchronous wrapper alone, the request and the wait hold the loop. |
| `commitlayer_crewai`, `commitlayer_x402` | In the thread that calls the hook: the `before_tool_call` hook and the `httpx` event hooks are synchronous functions. |

The bound is the client's: `Client(max_retries=3, max_retry_wait=10.0)` holds one recording for at most 30 seconds
of waiting. While the API states one second, as it does today, each retry waits one to three seconds: one retry is
the usual case, and all three are nine seconds at most. Where the work that
follows cannot be held that long (an activity with a short timeout, a signed payment with a short validity), build
the client with a smaller bound, `Client(max_retries=1, max_retry_wait=2.0)`, or with `max_retries=0` to turn
retrying off.

Python 3.10+. Both packages import without their runtime SDK installed (the SDK is imported lazily), so a
service that uses only one of them does not need the other.

```bash
pip install 'commitlayer-integrations[temporal,inngest]'
export COMMITLAYER_API_KEY=spk_...                                   # never log or print it
.venv/bin/python -m pytest integrations/python/tests -q              # no Temporal server, no Inngest dev server
```
