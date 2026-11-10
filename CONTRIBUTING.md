# Contributing

Issues and pull requests are welcome. Before opening a pull request:

- Run the tests for the package you changed (see the README's Development section). The Python tests must pass
  without any framework or runtime SDK installed; a new adapter imports its framework lazily, inside the hook.
- Keep the invariant every package holds: record the intent before the work runs, never send the work's result,
  never let the agent choose the contract version that judges it.
- Never put a real API key, trace id or customer identifier in code, tests or fixtures.
- A pull request that adds a package or changes a public interface also updates that package's README; the
  READMEs are rendered on commitlayer.ai/docs.

Releases are cut by the maintainers from tags; see the README.
