# CommitLayer for the OpenAI Agents SDK

Record function-tool calls as CommitLayer actions before they run.

```bash
pip install 'commitlayer-integrations[openai-agents]'
export COMMITLAYER_API_KEY=spk_...     # a recorder key
```

```python
from agents import Agent, Runner, function_tool
from commitlayer import Client
from commitlayer_integrations.tools import ToolRecorder
from commitlayer_openai_agents import CommitLayerHooks

recorder = ToolRecorder(Client(agent_id="support-bot"), MAPPING)
result = await Runner.run(agent, "Refund order 10231", hooks=CommitLayerHooks(recorder))
verdict = recorder.wait("refund:order_10231")  # the completion gate
```

`CommitLayerHooks` is a `RunHooks` whose `on_tool_start` records the mapped call from the `ToolContext` the SDK
passes function tools (`tool_name`, `tool_call_id`, the raw `tool_arguments`). Hosted tools pass a plain
context and are left alone. `on_tool_end` does nothing: the tool's own result is never evidence.

The mapping format is documented in `commitlayer_integrations.tools`.
