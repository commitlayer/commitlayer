"""The CommitLayer API surface each client package uses: methods, paths, request fields, response fields, headers.

This is an explicit manifest, not something scraped at run time. The packages build URLs from templates, pass
bodies through from the caller and read responses through helpers, so a scrape would either miss uses or invent
them. Instead `scripts/tests/test_api_surface.py` keeps the manifest honest against the code, both ways:

- every call site in a package's source (`_call(...)`, `request(...)`, `call(...)`, `api_url + "/v1/..."`, and an
  integration's call of an SDK method such as `record_action`) is in the manifest for that package, and every
  manifest entry has a call site;
- the bodies the Python SDK and recorder actually send, captured over a fake transport with every option set,
  carry exactly the fields the manifest lists, and the fields always sent are sent with no option set;
- the response fields the Python and TypeScript verdict readers actually touch, recorded through a tracking
  payload, are exactly the ones listed (the TypeScript side in scripts/tests/surface.test.mjs);
- every field name listed appears in the package's source.

`scripts/api_compat.py` reads this inventory and an OpenAPI diff and says which package a change touches.

    python scripts/api_surface.py            # the inventory as JSON
    python scripts/api_surface.py --markdown # as a table

Paths use the backend's OpenAPI templates (`/v1/actions/{action_ref}`); a field is dotted, with `[]` for the
items of a list (`facts[].source`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The headers every package sends, by package family. A request header the API starts requiring, or one it stops
# reading, is a change to the clients.
SDK_HEADERS = ("X-API-Key", "Content-Type", "Accept", "User-Agent")
RECORDER_HEADERS = ("x-api-key", "content-type", "accept", "user-agent")
# What every package reads from any answer: the error envelope of a 4xx/5xx and, on 429/503, Retry-After.
ERROR_FIELDS = ("error.code", "error.message", "error.details")
RESPONSE_HEADERS = ("Retry-After",)


@dataclass(frozen=True)
class Package:
    name: str  # the tag prefix publish.yml uses: sdk-python, recorder-js, ...
    directory: str
    ecosystem: str  # pypi | npm
    distribution: str  # the name on the registry
    sources: tuple[str, ...]  # source roots, tests excluded
    headers: tuple[str, ...]


PACKAGES: dict[str, Package] = {
    p.name: p
    for p in (
        Package("sdk-python", "sdk/python", "pypi", "commitlayer", ("sdk/python/commitlayer",), SDK_HEADERS),
        Package("sdk-js", "sdk/js", "npm", "commitlayer", ("sdk/js/src",), SDK_HEADERS),
        Package(
            "recorder-python",
            "recorder/python",
            "pypi",
            "commitlayer-mcp-recorder",
            ("recorder/python/commitlayer_mcp_recorder",),
            RECORDER_HEADERS,
        ),
        Package(
            "recorder-js", "recorder/js", "npm", "@commitlayer/mcp-recorder", ("recorder/js/src",), RECORDER_HEADERS
        ),
        Package(
            "integrations-python",
            "integrations/python",
            "pypi",
            "commitlayer-integrations",
            ("integrations/python",),  # every commitlayer_* package below it; tests/ is skipped
            SDK_HEADERS,  # through the Python SDK
        ),
        Package(
            "integrations-js",
            "integrations/js",
            "npm",
            "@commitlayer/integrations",
            ("integrations/js/src",),
            SDK_HEADERS,
        ),
    )
}


@dataclass(frozen=True)
class Use:
    """One endpoint as one package uses it."""

    package: str
    operation: str  # the package's own name for it
    method: str
    path: str
    request: tuple[str, ...] = ()  # body fields the package may send
    required: tuple[str, ...] = ()  # body fields it always sends
    query: tuple[str, ...] = ()
    response: tuple[str, ...] = ()  # fields it reads from a 2xx body
    sdk: tuple[str, ...] = field(default=(), compare=False)  # SDK method names that call it (Python, TypeScript)


ACTION_REQUIRED = ("action_id", "contract_id", "subject_id", "params", "requested_at")
ACTION_OPTIONAL = (
    "action_type",
    "subject_kind",
    "workflow_id",
    "agent_id",
    "channel",
    "compensates",
    "contract_version",
)
FACT_FIELDS = ("source", "subject_id", "subject_kind", "fact_type", "occurred_at", "attributes", "raw_ref")

# What the Python verdict reader (commitlayer_integrations.verdict.summarize_verification) and the LangGraph gate
# take from an ActionVerification, and what the TypeScript one (summarizeVerification in common.ts) takes.
VERDICT_FIELDS_PY = (
    "action.action_id",
    "action.contract_id",
    "action.contract_version",
    "action.latest.outcome",
    "action.latest.policy",
    "action.latest.settles_at",
    "action.latest.status",
    "contract.id",
    "contract.version",
    "lenses.outcome.reason_codes",
    "lenses.outcome.reason_labels",
    "lenses.outcome.verdict",
    "lenses.policy.reason_codes",
    "lenses.policy.reason_labels",
    "lenses.policy.verdict",
    "lenses.policy.verdict_hash",
    "settles_at",
    "status",
    "summary.headline",
)
VERDICT_FIELDS_JS = (
    "action.action_id",
    "lenses.outcome.reason_codes",
    "lenses.outcome.reason_labels",
    "lenses.outcome.verdict",
    "lenses.policy.reason_codes",
    "lenses.policy.reason_labels",
    "lenses.policy.verdict",
    "settles_at",
    "status",
    "summary.headline",
)


def _sdk(package: str) -> list[Use]:
    return [
        Use(
            package,
            "record_action",
            "POST",
            "/v1/actions",
            ACTION_REQUIRED + ACTION_OPTIONAL,
            ACTION_REQUIRED,
            sdk=("record_action", "recordAction"),
        ),
        Use(
            package,
            "push_facts",
            "POST",
            "/v1/facts",
            ("facts", "links", "watermarks"),
            ("facts",),
            sdk=("push_facts", "pushFacts"),
        ),
        # 0.1.4 (Python) / 0.1.5 (TypeScript): the evidence batch with source checkpoints.
        Use(
            package,
            "publish_batch",
            "POST",
            "/v1/evidence/batch",
            ("facts", "observed_through"),
            ("facts", "observed_through"),
            sdk=("publish_batch", "publishBatch"),
        ),
        Use(
            package,
            "verify",
            "POST",
            "/v1/actions/verify",
            ("dry_run", "action_id", "action", "facts", "links"),
            ("dry_run",),
        ),
        Use(package, "get_action", "GET", "/v1/actions/{action_ref}", sdk=("get_action", "getAction")),
        Use(
            package,
            "wait_for_settlement",
            "GET",
            "/v1/actions/{action_ref}/wait",
            query=("timeout",),
            response=("timed_out",),
            sdk=("wait_for_settlement", "waitForSettlement"),
        ),
        Use(
            package,
            "draft_contract",
            "POST",
            "/v1/contracts/draft",
            ("action_type", "contract_id", "lookback_days", "lookahead", "min_share"),
            ("action_type", "contract_id", "lookback_days", "lookahead", "min_share"),
        ),
        Use(package, "validate_contract", "POST", "/v1/contracts/validate", ("yaml",), ("yaml",)),
        Use(package, "save_contract", "POST", "/v1/contracts", ("yaml",), ("yaml",)),
        Use(package, "packs.list", "GET", "/v1/packs"),
        Use(package, "packs.get", "GET", "/v1/packs/{pack_id}"),
        Use(
            package, "packs.configure", "POST", "/v1/packs/{pack_id}/configure", ("configuration",), ("configuration",)
        ),
        Use(
            package,
            "packs.dry_run",
            "POST",
            "/v1/packs/{pack_id}/dry-run",
            ("configuration", "workflow"),
            ("configuration",),
        ),
        Use(
            package,
            "packs.install",
            "POST",
            "/v1/packs/{pack_id}/install",
            ("configuration", "confirm"),
            ("configuration", "confirm"),
        ),
        Use(package, "packs.coverage", "GET", "/v1/packs/{pack_id}/coverage"),
        Use(package, "packs.recorder_mapping", "GET", "/v1/packs/{pack_id}/recorder-mapping"),
    ]


def _recorder(package: str) -> list[Use]:
    required = ("action_id", "contract_id", "subject_id", "requested_at", "params", "channel")
    optional = ("contract_version", "workflow_id", "agent_id", "action_type", "subject_kind", "compensates")
    return [Use(package, "post_action", "POST", "/v1/actions", required + optional, required)]


def _integrations(package: str, verdict_fields: tuple[str, ...], record_reads: tuple[str, ...]) -> list[Use]:
    required = ("action_id", "contract_id", "subject_id", "requested_at", "params", "channel")
    return [
        Use(
            package,
            "record (activity, step, tool call, purchase)",
            "POST",
            "/v1/actions",
            required + ACTION_OPTIONAL,
            required,
            response=record_reads,
        ),
        Use(
            package,
            "x402 evidence",
            "POST",
            "/v1/facts",
            ("facts",) + tuple(f"facts[].{f}" for f in FACT_FIELDS),
            ("facts",) + tuple(f"facts[].{f}" for f in FACT_FIELDS),
        ),
        Use(package, "verdict poll", "GET", "/v1/actions/{action_ref}", response=verdict_fields),
        Use(
            package,
            "verdict poll on the completion gate",
            "GET",
            "/v1/actions/{action_ref}/wait",
            query=("timeout",),
            response=verdict_fields + (("timed_out",) if package.endswith("python") else ()),
        ),
    ]


USES: tuple[Use, ...] = tuple(
    _sdk("sdk-python")
    + _sdk("sdk-js")
    + _recorder("recorder-python")
    + _recorder("recorder-js")
    + _integrations("integrations-python", VERDICT_FIELDS_PY, ("action.action_id",))
    + _integrations("integrations-js", VERDICT_FIELDS_JS, ())
)


def uses(package: str | None = None) -> list[Use]:
    return [u for u in USES if package is None or u.package == package]


def inventory() -> dict:
    """The whole surface as JSON-ready data, sorted, so two runs on one commit are byte-identical."""
    out = []
    for name, pkg in sorted(PACKAGES.items()):
        out.append(
            {
                "package": name,
                "directory": pkg.directory,
                "ecosystem": pkg.ecosystem,
                "distribution": pkg.distribution,
                "request_headers": list(pkg.headers),
                "response_headers": list(RESPONSE_HEADERS),
                "error_fields": list(ERROR_FIELDS),
                "uses": [
                    {
                        k: (sorted(v) if isinstance(v, tuple) else v)
                        for k, v in asdict(u).items()
                        if k not in ("package", "sdk")
                    }
                    for u in sorted(uses(name), key=lambda u: (u.path, u.method))
                ],
            }
        )
    return {"schema_version": 1, "packages": out}


# ---------------- call sites in the code (the honesty checks read these) ----------------

_TEMPLATE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")
_CALL = re.compile(r"""(?:_call|\.request|\bcall)\(\s*"(GET|POST|PUT|PATCH|DELETE)"\s*,\s*f?["`]([^"`]+)["`]""")
_CONCAT = re.compile(r"""(?:\+\s*"|\}\s*)(/v1/[A-Za-z0-9_/{}.$-]+)""")
_SDK_CALL = re.compile(
    r"\.(record_action|recordAction|get_action|getAction|push_facts|pushFacts|wait_for_settlement|waitForSettlement)\("
)


def normalise_path(path: str) -> str:
    """`/v1/packs/${encodeURIComponent(id)}/configure?x=1` and `/v1/packs/{pack_id}/configure` are the same route."""
    return _TEMPLATE.sub("{}", path.split("?", 1)[0])


def source_files(package: str) -> list[Path]:
    files: list[Path] = []
    for root in PACKAGES[package].sources:
        for p in sorted((ROOT / root).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.suffix not in (".py", ".ts") or "/tests/" in rel or "/test/" in rel or "node_modules" in rel:
                continue
            files.append(p)
    return files


def call_sites(package: str) -> set[tuple[str | None, str]]:
    """(method, normalised path) for every call the package's source makes; method None when only the URL shows."""
    sdk_ops = {name: (u.method, normalise_path(u.path)) for u in uses("sdk-python") for name in u.sdk}
    found: set[tuple[str | None, str]] = set()
    for f in source_files(package):
        text = f.read_text(encoding="utf-8")
        for m in _CALL.finditer(text):
            found.add((m.group(1), normalise_path(m.group(2))))
        for m in _CONCAT.finditer(text):
            found.add((None, normalise_path(m.group(1))))
        if not package.startswith("sdk-"):
            for m in _SDK_CALL.finditer(text):
                found.add(sdk_ops[m.group(1)])
    return found


def to_markdown(inv: dict) -> str:
    lines = ["| Package | Method | Path | Request fields | Response fields read |", "|---|---|---|---|---|"]
    for p in inv["packages"]:
        for u in p["uses"]:
            lines.append(
                f"| {p['package']} | {u['method']} | `{u['path']}` | {', '.join(u['request']) or '—'} | "
                f"{', '.join(u['response']) or '— (returned as is)'} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The API surface the client packages use.")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)
    inv = inventory()
    sys.stdout.write(to_markdown(inv) if args.markdown else json.dumps(inv, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
