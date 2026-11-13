# CommitLayer for Pydantic AI

Record tool calls as CommitLayer actions before the tools run.

```bash
pip install 'commitlayer-integrations[pydantic-ai]'
export COMMITLAYER_API_KEY=spk_...     # a recorder key
```

```python
from pydantic_ai import Agent, FunctionToolset
from commitlayer import Client
from commitlayer_integrations.tools import ToolRecorder
from commitlayer_pydantic_ai import RecordingToolset

recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)
tools = FunctionToolset(tools=[refund_payment, close_ticket])
agent = Agent("openai:gpt-5", toolsets=[RecordingToolset(tools, recorder)])
```

`RecordingToolset` is a `WrapperToolset`: its `call_tool` records the mapped call with the validated arguments
and the run's `tool_call_id`, then delegates to the wrapped toolset. The tool's result is never sent.

`call_tool` runs on the event loop, and the client's request blocks, as does its wait when the API answers `429`
or `503` with `Retry-After` and the call is sent again. Since 0.1.3 the toolset records through
`ToolRecorder.record_async`, in a worker thread: the loop stays free and the tool is still called only once the
call is recorded. `Client(max_retries=0)` turns the retries off; see
[When the API is busy](../README.md#when-the-api-is-busy).

The mapping format is documented in `commitlayer_integrations.tools`.
