# Changelog: commitlayer (Python)

## 0.1.5

- `publish_batches(facts, links=..., observed_through=...)`: `POST /v1/evidence/batches`, the atomic evidence batch.
  Identity links, records and source checkpoints are validated together and stored in one transaction, or not at
  all: a rejected batch raises `CommitLayerError` with the code `batch_rejected`, `details.written` false, and every
  problem named (`invalid_sources`, `allowed_sources`, `invalid_fact_types`, `failed_links`, `failed_checkpoints`).
- `preflight_batches(...)`: `POST /v1/evidence/batches/preflight`, the same checks with nothing written, and the
  exact source ids the key publishes for (`source_bindings`).
- `publish_batch` (`POST /v1/evidence/batch`) is deprecated. It still works and still answers per item.

## 0.1.4

- The canonical environment names: `COMMITLAYER_RECORDER_KEY` (the agent's runtime) and `COMMITLAYER_EVIDENCE_KEY`
  (the evidence backend), with `COMMITLAYER_API_URL`, `COMMITLAYER_WORKSPACE_ID` and `COMMITLAYER_CONTRACT_ID`. They are
  the names of the in-product quickstart's two files, `commitlayer-recorder.env` and `commitlayer-evidence.env`.
- `commitlayer.recorder()` and `commitlayer.evidence()` build a client from those files' names. `Client()` with no key
  takes whichever key the process has and refuses a process that has both.
- `COMMITLAYER_API_KEY` still works, as a deprecated fallback that warns once.
- `record_action` takes its contract from `COMMITLAYER_CONTRACT_ID` when none is given.
- `publish_batch(facts=..., observed_through=...)`: `POST /v1/evidence/batch`, the records and a source
  checkpoint for every source scanned (an empty `facts` is a valid checkpoint-only batch).
- `__version__` is read from the installed distribution's metadata; a test installs the built package and checks it.

## 0.1.3

- Requests answered 429 or 503 with `Retry-After` are sent again, within a bounded wait.
