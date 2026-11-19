"""The guard refuses agent changes that must not merge themselves, and passes a proper release change."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import release_guard as rg  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """The package manifests and a few files, committed: the base the agent starts from."""
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    for p in rg.PACKAGES.values():
        manifest = "pyproject.toml" if p.ecosystem == "pypi" else "package.json"
        (r / p.directory).mkdir(parents=True, exist_ok=True)
        (r / p.directory / manifest).write_text((ROOT / p.directory / manifest).read_text())
        (r / p.directory / "README.md").write_text("readme\n")
        (r / p.directory / "tests").mkdir(exist_ok=True)
        (r / p.directory / "tests" / "t.txt").write_text("t\n")
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / ".github" / "workflows" / "publish.yml").write_text("name: publish\n")
    (r / "scripts").mkdir()
    (r / "scripts" / "api_surface.py").write_text("USES = ()\n")
    (r / "scripts" / "board_report.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def bump(repo: Path, package: str, to: str) -> None:
    p = rg.PACKAGES[package]
    if p.ecosystem == "pypi":
        f = repo / p.directory / "pyproject.toml"
        f.write_text(re.sub(r'(?m)^version = "[^"]+"$', f'version = "{to}"', f.read_text()))
    else:
        f = repo / p.directory / "package.json"
        f.write_text(re.sub(r'"version": "[^"]+"', f'"version": "{to}"', f.read_text(), count=1))


def current(repo: Path, package: str) -> str:
    """The version the fixture's manifest carries now (the repository's own), so these tests survive a release."""
    p = rg.PACKAGES[package]
    if p.ecosystem == "pypi":
        m = re.search(r'(?m)^version = "([^"]+)"$', (repo / p.directory / "pyproject.toml").read_text())
    else:
        m = re.search(r'"version": "([^"]+)"', (repo / p.directory / "package.json").read_text())
    assert m is not None
    return m.group(1)


def run(repo: Path):
    _git(repo, "add", "-A")
    return rg.check(repo, "HEAD")


def test_no_change_is_its_own_exit_code(repo):
    code, problems, _ = run(repo)
    assert code == rg.EXIT_EMPTY and "no change" in problems[0]


def test_a_proper_release_change_passes(repo):
    (repo / "sdk/python/README.md").write_text("readme with the new call\n")
    old = current(repo, "sdk-python")
    bump(repo, "sdk-python", "0.2.0")
    (repo / "scripts/api_surface.py").write_text("USES = ('new',)\n")
    (repo / "sdk/js/tests/t.txt").write_text("only a test changed: no release needed\n")
    code, problems, notes = run(repo)
    assert (code, problems) == (0, []) and notes == [f"sdk-python: {old} → 0.2.0"]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/publish.yml",
        ".github/prompts/api-release.md",
        "scripts/board_report.py",
        "sdk/js/.env",
        ".npmrc",
        "SECURITY.md",
        "recorder/js/LICENSE",
    ],
)
def test_forbidden_paths_are_refused(repo, path):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("changed\n")
    bump(repo, "recorder-js", "9.9.9")
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and any(f"`{path}` may not be changed" in p for p in problems)


def test_deleting_a_workflow_is_refused(repo):
    (repo / ".github/workflows/publish.yml").unlink()
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and any("publish.yml" in p for p in problems)


@pytest.mark.parametrize(
    "line",
    ["key = 'spk_" + "a" * 30 + "'", "Authorization: Bearer abcdefghijklmnopqrstu", "tok = 'ghp_" + "b" * 36 + "'"],
)
def test_secret_shaped_additions_are_refused(repo, line):
    (repo / "sdk/python/README.md").write_text(line + "\n")
    bump(repo, "sdk-python", "9.9.9")
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and any("secret" in p for p in problems)


@pytest.mark.parametrize("word", ["Written by claude-" + "test-1", "Son" + "net", "OP" + "US"])
def test_model_names_are_refused(repo, word):
    (repo / "integrations/js/README.md").write_text(f"{word}\n")
    bump(repo, "integrations-js", "9.9.9")
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and any("model name" in p for p in problems)


def test_a_shipped_change_without_a_version_bump_is_refused(repo):
    (repo / "recorder/python/README.md").write_text("new\n")
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and problems == [
        f"recorder-python: 1 shipped file(s) changed but the version is still {current(repo, 'recorder-python')}"
    ]


def test_a_version_that_goes_down_is_refused(repo):
    bump(repo, "sdk-js", "0.1.0")
    code, problems, _ = run(repo)
    assert code == rg.EXIT_REFUSED and any("went down" in p for p in problems)


def test_cli_writes_the_summary(repo, tmp_path):
    old = current(repo, "sdk-js")
    bump(repo, "sdk-js", "9.9.9")
    (repo / "sdk/js/README.md").write_text("x\n")
    _git(repo, "add", "-A")
    out = tmp_path / "guard.md"
    assert rg.main(["--repo", str(repo), "--base", "HEAD", "--summary", str(out)]) == 0
    assert out.read_text().startswith("## Release guard: passed") and f"sdk-js: {old} → 9.9.9" in out.read_text()
