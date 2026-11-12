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

Python 3.10+. Both packages import without their runtime SDK installed (the SDK is imported lazily), so a
service that uses only one of them does not need the other.

```bash
pip install 'commitlayer-integrations[temporal,inngest]'
export COMMITLAYER_API_KEY=spk_...                                   # never log or print it
.venv/bin/python -m pytest integrations/python/tests -q              # no Temporal server, no Inngest dev server
```
