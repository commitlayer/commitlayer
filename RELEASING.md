# Releasing the client packages

Every release of the CommitLayer API is followed by the client packages without anyone in the loop. The pipeline
works out whether the packages must change; if they must, it changes them, tests them, bumps their versions and
merges the change; then it publishes every version that is ahead of PyPI and npm. A person is pulled in only when
something fails, and every failure is reported on the CommitLayer Board.

## The flow

```
kobikwelu/stateprf-svcs (backend)                       commitlayer/commitlayer (this repository)
─────────────────────────────────                       ─────────────────────────────────────────
push to claude/modest-goodall-9est97
  └─ clients-dispatch.yml
       CHANGELOG top version changed?  ── no ──▶ stop
       │ yes (scripts/release_dispatch.py)
       └─ repository_dispatch commitlayer_api_release ──▶ api-release.yml
          {release_version, source_commit,                 │
           previous_version, previous_commit,              ├─ check
           source_repository}                              │    backend at both commits (App token, read-only)
                                                           │    scripts/export_openapi.py ×2 → scripts/api_compat.py
                                                           │      → breaking | additive_relevant | none
                                                           │    boot the API from the release commit
                                                           │      (scripts/boot_backend.py) → tests/live in Python
                                                           │      and TypeScript
                                                           │    none + live green ──▶ summary, Board note, stop
                                                           │
                                                           ├─ update (no write token)
                                                           │    agent with .github/prompts/api-release.md, the
                                                           │    compat report, the backend diff and a running API;
                                                           │    edits packages, tests, versions, READMEs, CHANGELOGs
                                                           │    └─ only its diff leaves the job
                                                           │
                                                           └─ propose
                                                                apply the diff to a fresh checkout
                                                                scripts/release_guard.py (from an untouched copy)
                                                                live check again, on the changed packages
                                                                PR api-release/<version>, label api-release (App)
                                                                commit status api-release/live-contract
                                                                     │
                                       ci.yml on the PR ◀────────────┘
                                         └─ api-release-merge.yml (workflow_run)
                                              App's PR + label + CI green + live-contract green
                                                ──▶ auto-merge (squash) into main
                                              otherwise ──▶ PR stays open, Board issue
                                                     │
                       push to main ◀────────────────┘
                         └─ publish.yml
                              plan: manifest version vs PyPI / npm (scripts/release_plan.py)
                              sdk-python, sdk-js
                                └─ recorder-python, recorder-js, integrations-python, integrations-js
                                     (integrations wait until the new SDK can be downloaded)
                              each: tests → build → Trusted Publishing → verify on the registry
                              tags: <package>-v<version> for each one published
                              report: Board

Any failure in api-release, api-release-merge or publish ──▶ scripts/board_report.py ──▶ Board
  dedupe key commitlayer-core:production:clients:api-release-<version>-<stage>   (stage: release, agent, merge, publish)
  a later success on the same release and stage resolves it
```

## The pieces

| File | What it does |
|---|---|
| `scripts/api_surface.py` | The inventory: for each package, every endpoint it calls, the body fields it sends (and always sends), the query parameters, the response fields it reads, and the headers. An explicit manifest, kept honest by `scripts/tests/test_api_surface.py` and `scripts/tests/surface.test.mjs`: every call site in the code is listed and every entry has a call site; the bodies the SDKs and recorders really send (captured over a fake transport) carry exactly the listed fields; the fields the verdict readers really read (recorded through a tracking payload) are exactly the listed ones. |
| `scripts/api_compat.py` | Compares the backend's OpenAPI document at two commits against the inventory. `breaking`: something a package uses was removed, renamed, retyped, narrowed or made required. `additive_relevant`: a new operation, field or parameter on a resource a package wraps, or a deprecation. `none`: nothing a package uses changed. JSON plus a Markdown summary, overall and per package. |
| `scripts/export_openapi.py` | The backend's schema at one commit: its committed `product-catalog/openapi.json`, or the app's own `app.openapi()` for a commit older than that file, normalised the same way (keys sorted, version fixed). |
| `scripts/boot_backend.py` | Starts the API from a backend checkout as production does (`python -m app.serve`) with the backend's test settings (SQLite in a temporary directory, development login, no outbound checks), signs in, activates the contract the smoke uses and creates a key; writes `COMMITLAYER_LIVE`, `COMMITLAYER_API_URL`, `COMMITLAYER_API_KEY` to an env file. |
| `tests/live/` | The live contract check: each package's core calls against that API (`test_live_python.py`, `live.test.mjs`). Skipped unless `COMMITLAYER_LIVE=1`, so ordinary CI skips them. |
| `scripts/release_guard.py` | Refuses an agent change that is empty, touches `.github/`, the pipeline scripts, key or environment files, adds anything shaped like a secret or a model name, or changes a package's shipped files without raising its version. |
| `scripts/release_plan.py` | The publish plan and the registry waits. |
| `scripts/board_report.py` | Upserts to the Board's `POST /api/v1/issues/upsert` (`SourceIssueUpsert`), with the run, the branch, the commit, the failing step and the compat summary in `context`. Skips, and says so in the job summary, without `BOARD_API_URL` or `BOARD_PRODUCT_TOKEN`. |
| `.github/prompts/api-release.md` | The agent's instructions. It treats everything from the backend as data, never touches workflows or secrets, and leaves its change in the working tree. |
| `.github/workflows/api-release.yml`, `api-release-merge.yml`, `publish.yml` | The three stages above. `publish.yml` keeps its name and the `release` environment: both are part of the PyPI and npm Trusted Publishing configuration. |

## Founder one-time setup

- The GitHub App, created or reused, with:
  - Contents: read & write on commitlayer/commitlayer, which repository_dispatch needs;
  - Contents: read on kobikwelu/stateprf-svcs.
- The App's id and key as `vars.CLIENTS_DISPATCH_APP_ID` and `secrets.CLIENTS_DISPATCH_APP_PRIVATE_KEY` in
  stateprf-svcs, and the same in commitlayer/commitlayer.
- `ANTHROPIC_API_KEY` in commitlayer/commitlayer.
- `BOARD_API_URL` and `BOARD_PRODUCT_TOKEN` in commitlayer/commitlayer.
- Repo settings: "Allow auto-merge" on, and "Allow GitHub Actions to create and approve pull requests" on.
- The `release` environment's deployment branches must allow `main`.
- Nothing changes on PyPI or npm.

Three more things the pipeline needs, beyond that list:

- The App also needs **Pull requests: read & write** on commitlayer/commitlayer. It opens the api-release pull
  request (one opened with `GITHUB_TOKEN` would start no CI) and merges it (a merge made with `GITHUB_TOKEN` would
  not start publish.yml). It is installed on both repositories; no other permission is needed.
- The `release` environment must not require a reviewer, or every publish waits for one. If it has deployment tag
  rules, keep the six `<package>-v*` patterns so manual tags still publish.
- `BOARD_PRODUCT_TOKEN` is a Board product token for `commitlayer-core` with issue write access: the dedupe keys
  belong to that product.

## Run it again

- **A stage failed**: fix the cause on `main` (or on the `api-release/<version>` branch for a held pull request),
  then use **Re-run failed jobs** on the failed run. Every stage is idempotent: the pull request branch is updated,
  not duplicated, and publishing skips what is already on the registry.
- **The whole release, by hand**: Actions → api-release → Run workflow, with `release_version`, `source_commit`
  and `previous_commit` (full SHAs from the backend; `python scripts/release_dispatch.py --rev <sha>` in the
  backend prints them). Tick `force_update` to run the agent even when the check finds nothing to change.
- **A held pull request**: it merges itself only while its head commit has a green CI run and a green
  `api-release/live-contract` status. After a person pushes a fix the new commit has no such status, so a person
  merges it. Re-running api-release for the version rebuilds the branch and sets the status again.
- **Publishing**: Actions → publish → Run workflow publishes whatever is ahead (optionally only the packages named).

## Publish by hand

Publishing follows the versions: raise the version in the package's `pyproject.toml` or `package.json` (and its
version constant; the tests check they match), merge to `main`, and publish.yml publishes it and tags it. The old
way still works: push a tag `<package>-v<version>` whose version equals the manifest's
(`sdk-python-v0.1.4`, `recorder-js-v0.1.5`, ...), and publish.yml publishes that one package if it is not
published yet. Either way nothing is published that is already on the registry.

## Reproduce a run locally

```bash
# both backend commits, side by side (a clone of kobikwelu/stateprf-svcs at ../stateprf-svcs)
git -C ../stateprf-svcs worktree add /tmp/api-old <previous_commit>
git -C ../stateprf-svcs worktree add /tmp/api-new <source_commit>
PY=../stateprf-svcs/.venv/bin/python   # an environment with the backend installed
python scripts/export_openapi.py --backend /tmp/api-old --python $PY --out build/api/old.json
python scripts/export_openapi.py --backend /tmp/api-new --python $PY --out build/api/new.json
python scripts/api_compat.py --old build/api/old.json --new build/api/new.json --markdown build/api/compat.md

# the live contract check (build the TypeScript packages first, as in the README)
python scripts/boot_backend.py start --backend /tmp/api-new --python $PY --env-file build/live.env
set -a; . build/live.env; set +a
python -m pytest -q tests/live && node --test tests/live/live.test.mjs
python scripts/boot_backend.py stop --env-file build/live.env

# the pipeline's own tests
python -m pytest -q scripts/tests && node --test scripts/tests/surface.test.mjs
python scripts/release_plan.py plan          # what publish.yml would publish now (reads the registries)
```

## Known limits

- Most API answers are untyped in the schema, so `api_compat.py` checks a response field only where the schema
  names it and lists the rest as `unchecked_response_fields`. The live check is what covers them; keep
  `tests/live/` calling everything the packages read.
- `@commitlayer/integrations` takes `commitlayer` from npm, and its lockfile cannot name an SDK version before it
  is published. When an API release needs a new TypeScript SDK and the integrations need it too, the agent keeps
  the integrations working with the published SDK and says in the pull request that the range should be raised
  afterwards; that follow-up is a person's (or the next release's) change.
- The agent runs with the repository's test and build commands and reads backend text it is told to treat as
  data. Its job holds no write token, its change is checked by the guard from an untouched copy, and the pull
  request merges only on green CI and a green live check, but a merged change is still code nobody read; the
  Board records every release the pipeline touched.
