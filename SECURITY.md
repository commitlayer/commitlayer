# Security

To report a vulnerability in any package in this repository, email security@commitlayer.ai. Please do not open a
public issue for a security finding. We acknowledge reports within two business days and say what we will do.

What these packages handle:

- An API key, read from `COMMITLAYER_API_KEY` or passed to the client. It is sent only as a bearer token to the
  API URL configured, defaulting to `https://api.commitlayer.ai`, and is never logged, printed or persisted.
- The arguments of the tool calls, activities or steps an agent makes, sent as an action's parameters before the
  work runs. Results of the work are never sent.

Releases are published through Trusted Publishing on PyPI and npm from the `publish` workflow in this repository,
with provenance attestations; no registry token exists. Verify an npm package with `npm audit signatures` and a
PyPI package through its attestations on the project page.
