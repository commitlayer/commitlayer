# CommitLayer client packages

The SDKs, the MCP recorder and the framework and workflow-runtime integrations for
[CommitLayer](https://commitlayer.ai): record what an agent is about to do as an action under a contract, then
read the verdict CommitLayer reaches by checking the systems of record. The agent's own result is never the
evidence.

| Package | What it is | Install |
|---|---|---|
| [`commitlayer`](sdk/python/README.md) (PyPI) | Python SDK: client, `@action` decorator, watermarks, contract drafting | `pip install commitlayer` |
| [`commitlayer`](sdk/js/README.md) (npm) | TypeScript/JavaScript SDK, standard library only | `npm install commitlayer` |
| [`commitlayer-mcp-recorder`](recorder/python/README.md) | A stdio proxy in front of any MCP server that records a mapped tool call as an action before forwarding it (Python) | `pip install commitlayer-mcp-recorder` |
| [`@commitlayer/mcp-recorder`](recorder/js/README.md) | The same recorder in TypeScript | `npm install -g @commitlayer/mcp-recorder` |
| [`commitlayer-integrations`](integrations/python/README.md) | LangGraph and LangChain, CrewAI, OpenAI Agents SDK, Pydantic AI, Temporal and Inngest (Python) | `pip install 'commitlayer-integrations[langgraph]'` and so on |
| [`@commitlayer/integrations`](integrations/js/README.md) | Temporal and Inngest (TypeScript) | `npm install @commitlayer/integrations` |

Every package does the same two things in its runtime's own terms:

1. **Record the intent before the work runs**: `POST /v1/actions` with the contract, the subject and the parameters
   taken from the call's own arguments. The result of the work is never sent.
2. **Read the verdict**: `GET /v1/actions/{ref}` until the verification settles, then branch on PASS, FAIL or
   UNKNOWN. UNKNOWN means the systems of record could not be read, never that the action probably worked.

The agent may declare its intent and may read the verdict. It never controls the facts, the governing contract or
the final evidence.

## Documentation

- [Getting started](https://commitlayer.ai/docs/quickstart)
- [API reference](https://commitlayer.ai/docs/api)
- [Action contracts](https://commitlayer.ai/docs/action-contracts)
- Each package's README above is also rendered at `commitlayer.ai/docs/<package path>`, for example
  [commitlayer.ai/docs/sdk/python](https://commitlayer.ai/docs/sdk/python).

## Development

Python 3.10+ and Node 18+. The Python tests run without any framework or runtime SDK installed; the two optional
runtimes are needed only for their own test files.

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest ./sdk/python ./recorder/python './integrations/python[temporal,inngest]'
python -m pytest -q integrations/python/tests recorder/python/tests

for d in sdk/js recorder/js integrations/js; do (cd $d && npm ci && npm run build && npm test); done
```

An API key is read from `COMMITLAYER_API_KEY` and is never logged, printed or written to a fixture. Nothing in
this repository needs one: the tests use a fake transport.

## Releases

A release is a tag, `<package>-v<version>`, whose version equals the one in the package's `pyproject.toml` or
`package.json`: `sdk-python-v0.1.1`, `recorder-python-v0.1.1`, `integrations-python-v0.1.1`, `sdk-js-v0.1.1`,
`recorder-js-v0.1.1`, `integrations-js-v0.1.1`. The publish workflow runs the tests, builds, and publishes through
Trusted Publishing on PyPI and npm with provenance. No registry token exists anywhere.

## Security

To report a vulnerability, email security@commitlayer.ai. See [SECURITY.md](SECURITY.md).

## Licence

MIT, see [LICENSE](LICENSE). Copyright Asterfold Labs, Inc.
