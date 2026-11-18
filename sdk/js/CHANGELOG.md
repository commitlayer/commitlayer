# Changelog: commitlayer (JavaScript / TypeScript)

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
