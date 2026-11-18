"""The gate between the release agent's edits and a pull request that can merge itself.

api-release.yml runs the agent in a job that holds no write token and keeps only its diff. The job that opens
the pull request applies that diff to a fresh checkout, stages it, and runs this script from a pristine copy of
the repository (so nothing the agent edited decides whether its own edit passes):

    python .pipeline/scripts/release_guard.py --repo . --base "$GITHUB_SHA" --summary build/guard.md

It refuses the change, with every reason, when:

- there is no change at all (exit 3: the release needed one and the agent made none);
- it touches a path the agent may not: workflows and anything else under .github/, the pipeline's scripts (all of
  scripts/ except the inventory, scripts/api_surface.py, which must follow a new call), environment and key
  files, LICENSE, SECURITY.md;
- an added line holds something shaped like a secret or names a model;
- a package's shipped files changed and its version did not go up (the publish workflow would skip it), or a
  version went down.

Exit 0 when the change may be proposed. Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_surface import PACKAGES  # noqa: E402
from board_report import _PATTERNS as SECRET_PATTERNS  # noqa: E402

FORBIDDEN = (
    ".github/*",
    "scripts/*",
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    ".npmrc",
    "*/.npmrc",
    ".pypirc",
    "LICENSE",
    "*/LICENSE",
    "SECURITY.md",
)
ALLOWED = ("scripts/api_surface.py",)
# Spelled with character classes so this file does not itself contain the names it refuses.
MODEL_NAMES = re.compile(r"(?i)\bcl[a]ude-[a-z0-9][a-z0-9.-]*|\b(?:op[u]s|son[n]et|hai[k]u)\b")
# Files in a package directory that are not shipped: changing only these needs no release.
UNSHIPPED = ("*/tests/*", "*/test/*", "*/package-lock.json", "*/tsconfig.test.json", "*/.gitignore")

EXIT_REFUSED, EXIT_EMPTY = 1, 3


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for the staged change against `base`; a rename counts as both paths."""
    out = []
    for line in git(repo, "diff", "--cached", "--name-status", "-M", base).splitlines():
        parts = line.split("\t")
        out.append((parts[0][0], parts[-1]))
        if parts[0].startswith("R"):
            out.append(("D", parts[1]))
    return out


def added_lines(repo: Path, base: str) -> list[tuple[str, str]]:
    path, out = "", []
    for line in git(repo, "diff", "--cached", "--unified=0", base).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def _version_tuple(v: str) -> tuple:
    main, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in main.split("."))
    return nums + ((1,) if not pre else (0, pre))


def version_at(repo: Path, rev: str | None, package: str) -> str | None:
    p = PACKAGES[package]
    name = "pyproject.toml" if p.ecosystem == "pypi" else "package.json"
    rel = f"{p.directory}/{name}"
    try:
        text = git(repo, "show", f"{rev}:{rel}") if rev else git(repo, "show", f":{rel}")
    except subprocess.CalledProcessError:
        return None
    if p.ecosystem == "pypi":
        m = re.search(r'(?m)^version = "([^"]+)"$', text)
        return m.group(1) if m else None
    import json

    return json.loads(text).get("version")


def check(repo: Path, base: str) -> tuple[int, list[str], list[str]]:
    """(exit code, problems, notes)."""
    files = changed_files(repo, base)
    if not files:
        return EXIT_EMPTY, ["the agent made no change, and the release needed one"], []
    problems: list[str] = []
    notes: list[str] = []
    for status, path in files:
        if any(fnmatch.fnmatch(path, pat) for pat in FORBIDDEN) and path not in ALLOWED:
            problems.append(f"`{path}` may not be changed by the release agent ({status})")
    for path, line in added_lines(repo, base):
        for rx, _ in SECRET_PATTERNS:
            if rx.search(line):
                problems.append(f"`{path}` adds something shaped like a secret")
                break
        if MODEL_NAMES.search(line):
            problems.append(f"`{path}` adds a model name: {MODEL_NAMES.search(line).group(0)!r}")
    for name, p in sorted(PACKAGES.items()):
        shipped = [
            path
            for _, path in files
            if path.startswith(p.directory + "/") and not any(fnmatch.fnmatch(path, pat) for pat in UNSHIPPED)
        ]
        before, after = version_at(repo, base, name), version_at(repo, None, name)
        if before and after and _version_tuple(after) < _version_tuple(before):
            problems.append(f"{name}: version went down, {before} → {after}")
        if shipped and before == after:
            problems.append(f"{name}: {len(shipped)} shipped file(s) changed but the version is still {after}")
        elif before != after:
            notes.append(f"{name}: {before} → {after}")
    return (EXIT_REFUSED if problems else 0), sorted(set(problems)), notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refuse a release agent change that may not merge itself.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--summary", default=None)
    args = ap.parse_args(argv)
    code, problems, notes = check(Path(args.repo).resolve(), args.base)
    lines = ["## Release guard: " + ("passed" if code == 0 else "refused"), ""]
    lines += [f"- {n}" for n in notes] or (["- no package version changed"] if code == 0 else [])
    lines += [f"- **refused**: {p}" for p in problems]
    text = "\n".join(lines) + "\n"
    if args.summary:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(text, encoding="utf-8")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
