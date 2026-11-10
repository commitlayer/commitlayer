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

The mapping format is documented in `commitlayer_integrations.tools`.
