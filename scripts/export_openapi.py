"""The backend's OpenAPI document at one commit, in the form the compatibility check compares.

From 2026-11-18 the backend commits it as `product-catalog/openapi.json` (keys sorted, `info.version` fixed to
`unversioned`, so a version bump is not a diff). This script prefers that file; for an older commit that has no
such file it imports the FastAPI app from the checkout and writes the same normalised form. Run it with the
interpreter that has the backend installed:

    ../stateprf-svcs/.venv/bin/python scripts/export_openapi.py --backend ../stateprf-svcs --out build/new.json

The import happens in a child process whose working directory is the checkout, so the checkout's own `app`
package is the one imported, whatever else is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

UNVERSIONED = "unversioned"

_DUMP = (
    "import json, sys\nsys.path.insert(0, '.')\nfrom app.api.main import app\njson.dump(app.openapi(), sys.stdout)\n"
)


def normalise(spec: dict) -> dict:
    spec = json.loads(json.dumps(spec))
    spec.setdefault("info", {})["version"] = UNVERSIONED
    return spec


def dump(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export(backend: Path, python: str) -> tuple[dict, str]:
    committed = backend / "product-catalog" / "openapi.json"
    if committed.is_file():
        return normalise(json.loads(committed.read_text(encoding="utf-8"))), "committed product-catalog/openapi.json"
    r = subprocess.run([python, "-c", _DUMP], cwd=backend, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise SystemExit(f"could not import the app from {backend}:\n{r.stderr[-2000:]}")
    return normalise(json.loads(r.stdout)), "generated from app.openapi()"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    # A relative interpreter path is made absolute (not resolved: a virtualenv's python is a symlink), because the
    # import runs with the checkout as its working directory.
    python = os.path.abspath(args.python) if os.sep in args.python else args.python
    spec, how = export(Path(args.backend).resolve(), python)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(dump(spec), encoding="utf-8")
    print(f"{args.out}: {len(spec.get('paths') or {})} paths, {how}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
