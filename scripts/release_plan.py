"""Which client packages need publishing: the manifest version of each, against PyPI and npm.

publish.yml is version-driven. Its `plan` job runs

    python scripts/release_plan.py plan [--tag sdk-python-v0.1.4] [--only sdk-js,recorder-js] \
        --github-output "$GITHUB_OUTPUT"

and publishes exactly the packages whose `pyproject.toml` / `package.json` version is not on the registry yet:
`https://pypi.org/pypi/<name>/<version>/json` and `https://registry.npmjs.org/<name>/<version>` answer 404 for a
version never published. A version that is already there is skipped, so running the workflow again is a no-op.
Any other answer (a registry outage, a 5xx) fails the plan rather than guess.

With `--tag` (a manual `<package>-v<version>` tag, as before) only that package is planned, and its manifest
version must equal the tag's.

    python scripts/release_plan.py wait --package sdk-python --version 0.1.4 --timeout 600

polls until that version can be downloaded (PyPI: the JSON API and the simple index; npm: the version document),
for a dependent package that must not publish before it, and to verify a publish.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_surface import PACKAGES, ROOT  # noqa: E402

# Publish order: the SDKs first; recorder and integrations after them (integrations-python requires the SDK).
ORDER = ("sdk-python", "sdk-js", "recorder-python", "recorder-js", "integrations-python", "integrations-js")
USER_AGENT = "commitlayer-release-plan/1 (+https://github.com/commitlayer/commitlayer)"

Fetch = Callable[[str], tuple[int, str]]


def http_get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed registry hosts
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def manifest_version(package: str, root: Path = ROOT) -> str:
    d = root / PACKAGES[package].directory
    if PACKAGES[package].ecosystem == "pypi":
        # A pattern, not tomllib: the same reading the packages' own tests use on Python 3.10.
        m = re.search(r'(?m)^version = "([^"]+)"$', (d / "pyproject.toml").read_text(encoding="utf-8"))
        if not m:
            raise SystemExit(f"{package}: no version in pyproject.toml")
        return m.group(1)
    return json.loads((d / "package.json").read_text(encoding="utf-8"))["version"]


def registry_url(package: str, version: str) -> str:
    p = PACKAGES[package]
    if p.ecosystem == "pypi":
        return f"https://pypi.org/pypi/{p.distribution}/{version}/json"
    return f"https://registry.npmjs.org/{p.distribution}/{version}"


class RegistryError(RuntimeError):
    pass


def published(package: str, version: str, fetch: Fetch = http_get) -> bool:
    status, _ = fetch(registry_url(package, version))
    if status == 200:
        return True
    if status == 404:
        return False
    raise RegistryError(f"{registry_url(package, version)} answered {status}")


def downloadable(package: str, version: str, fetch: Fetch = http_get) -> bool:
    """Published and visible where an installer looks: for PyPI also the simple index pip reads."""
    if not published(package, version, fetch):
        return False
    p = PACKAGES[package]
    if p.ecosystem != "pypi":
        return True
    status, page = fetch(f"https://pypi.org/simple/{p.distribution}/")
    if status != 200:
        return False
    stems = {p.distribution, p.distribution.replace("-", "_")}
    return any(f"{s}-{version}" in page for s in stems)


def parse_tag(tag: str) -> tuple[str, str]:
    package, sep, version = tag.rpartition("-v")
    if not sep or package not in PACKAGES or not version:
        raise SystemExit(f"unknown package in tag {tag}")
    return package, version


def plan(tag: str | None = None, only: list[str] | None = None, fetch: Fetch = http_get, root: Path = ROOT) -> dict:
    names = list(ORDER)
    tag_version = None
    if tag:
        package, tag_version = parse_tag(tag)
        names = [package]
    elif only:
        unknown = sorted(set(only) - set(PACKAGES))
        if unknown:
            raise SystemExit(f"unknown packages: {', '.join(unknown)}")
        names = [n for n in ORDER if n in only]
    rows = []
    for name in names:
        version = manifest_version(name, root)
        if tag_version is not None and version != tag_version:
            raise SystemExit(f"{name}: manifest version {version} != tag {tag_version}")
        is_published = published(name, version, fetch)
        p = PACKAGES[name]
        rows.append(
            {
                "package": name,
                "directory": p.directory,
                "ecosystem": p.ecosystem,
                "distribution": p.distribution,
                "version": version,
                "published": is_published,
                "publish": not is_published,
                "tag": f"{name}-v{version}",
            }
        )
    return {"packages": rows, "to_publish": [r["package"] for r in rows if r["publish"]]}


def github_output(result: dict) -> str:
    lines = [f"any={'true' if result['to_publish'] else 'false'}", f"packages={json.dumps(result['to_publish'])}"]
    planned = {r["package"]: r for r in result["packages"]}
    for name in ORDER:
        key = name.replace("-", "_")
        r = planned.get(name)
        lines.append(f"publish_{key}={'true' if r and r['publish'] else 'false'}")
        lines.append(f"version_{key}={r['version'] if r else manifest_version(name)}")
    return "\n".join(lines) + "\n"


def summary(result: dict) -> str:
    lines = [
        "## Publish plan",
        "",
        "| Package | Registry name | Version | On the registry | Action |",
        "|---|---|---|---|---|",
    ]
    for r in result["packages"]:
        lines.append(
            f"| {r['package']} | `{r['distribution']}` ({r['ecosystem']}) | {r['version']} | "
            f"{'yes' if r['published'] else 'no'} | {'publish' if r['publish'] else 'skip'} |"
        )
    if not result["to_publish"]:
        lines += ["", "Every version is already published; nothing to do."]
    return "\n".join(lines) + "\n"


def wait(
    package: str,
    version: str,
    timeout: float,
    interval: float = 10.0,
    fetch: Fetch = http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = clock() + timeout
    while True:
        try:
            if downloadable(package, version, fetch):
                return True
        except RegistryError as e:
            print(f"retrying: {e}", file=sys.stderr)
        if clock() >= deadline:
            return False
        sleep(min(interval, max(0.0, deadline - clock())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan, or wait for, client package publishes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--tag", default=None, help="a <package>-v<version> tag: plan only that package")
    p.add_argument("--only", default="", help="comma-separated package names (workflow_dispatch)")
    p.add_argument("--github-output", default=None)
    p.add_argument("--summary", default=None)
    w = sub.add_parser("wait")
    w.add_argument("--package", required=True, choices=sorted(PACKAGES))
    w.add_argument("--version", default=None, help="default: the manifest version")
    w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--interval", type=float, default=10.0)
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        try:
            result = plan(args.tag or None, only or None)
        except RegistryError as e:
            print(f"::error::{e}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as f:
                f.write(github_output(result))
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as f:
                f.write(summary(result))
        return 0
    version = args.version or manifest_version(args.package)
    if wait(args.package, version, args.timeout, args.interval):
        print(f"{PACKAGES[args.package].distribution} {version} is downloadable")
        return 0
    print(
        f"::error::{PACKAGES[args.package].distribution} {version} was not downloadable after {args.timeout:.0f} s",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
