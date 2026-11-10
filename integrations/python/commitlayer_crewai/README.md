# CommitLayer for CrewAI

Record a crew's tool calls as CommitLayer actions before the tools run.

```bash
pip install 'commitlayer-integrations[crewai]'
export COMMITLAYER_API_KEY=spk_...     # a recorder key
```

```python
from commitlayer import Client
from commitlayer_integrations.tools import ToolRecorder
import commitlayer_crewai

recorder = ToolRecorder(Client(agent_id="ops-crew"), MAPPING)  # MAPPING: tool name -> contract, subject, params
commitlayer_crewai.install(recorder)  # registers a before_tool_call hook
result = crew.kickoff()
verdict = recorder.wait("refund:order_10231")  # the completion gate
```

`install` registers CrewAI's `before_tool_call` hook with a function that records the mapped calls from the
hook context's `tool_name` and `tool_input` and returns `None`, so no call is ever blocked. `tools=[...]` and
`agents=[...]` narrow the hook the way CrewAI's own decorator does; `uninstall(recorder)` removes it. The
tool's result is never sent to CommitLayer.

The mapping format is documented in `commitlayer_integrations.tools`.
