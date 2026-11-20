# Changelog: commitlayer (JavaScript / TypeScript)

## 0.1.6

- `publishBatches({ facts, links, observedThrough })`: `POST /v1/evidence/batches`, the atomic evidence batch.
  Identity links, records and source checkpoints are validated together and stored in one transaction, or not at
  all: a rejected batch throws `CommitLayerError` with the code `batch_rejected`, `details.written` false, and every
  problem named (`invalid_sources`, `allowed_sources`, `invalid_fact_types`, `failed_links`, `failed_checkpoints`).
- `preflightBatches(...)`: `POST /v1/evidence/batches/preflight`, the same checks with nothing written, and the
  exact source ids the key publishes for (`source_bindings`).
- `publishBatch` (`POST /v1/evidence/batch`) is deprecated. It still works and still answers per item.

## 0.1.5

- Dual package: ESM (`import`) and CommonJS (`require`), each with its types, through `exports`. Node 20 or later.
- `VERSION` is generated from package.json at build time; a test packs the package, installs the tarball in a new
  project and checks `VERSION` from both `require` and `import`.
- The canonical environment names: `COMMITLAYER_RECORDER_KEY` and `COMMITLAYER_EVIDENCE_KEY`, with
  `COMMITLAYER_API_URL`, `COMMITLAYER_WORKSPACE_ID` and `COMMITLAYER_CONTRACT_ID` (`ENV` exports them). `recorder()` and
  `evidence()` build a client from them; `new Client()` refuses a process that has both keys.
- `COMMITLAYER_API_KEY` still works, as a deprecated fallback that warns once.
- `recordAction(null, subjectId, ...)` takes its contract from `COMMITLAYER_CONTRACT_ID`.
- `publishBatch({ facts, observedThrough })`: `POST /v1/evidence/batch`, the records and a checkpoint for every source
  scanned (an empty `facts` is a valid checkpoint-only batch).

## 0.1.4

- Requests answered 429 or 503 with `Retry-After` are sent again, within a bounded wait.
