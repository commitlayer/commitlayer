"""publish.yml's plan: publish what the registries do not have, nothing else; a rerun is a no-op."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import release_plan as rp  # noqa: E402


class Registry:
    """A fake PyPI and npm: `have` maps a registry URL to 200; everything else is 404 unless listed in `status`."""

    def __init__(self, have=(), status=None, simple=None):
        self.have, self.status, self.simple, self.calls = set(have), dict(status or {}), dict(simple or {}), []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.status:
            return self.status[url], ""
        if url.startswith("https://pypi.org/simple/"):
            return (200, self.simple[url]) if url in self.simple else (404, "")
        return (200, "{}") if url in self.have else (404, "")


@pytest.fixture
def repo(tmp_path):
    """A copy of the six manifests at known versions."""
    for p in rp.PACKAGES.values():
        src = ROOT / p.directory / ("pyproject.toml" if p.ecosystem == "pypi" else "package.json")
        dst = tmp_path / p.directory / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    return tmp_path


def _versions(root):
    return {n: rp.manifest_version(n, root) for n in rp.PACKAGES}


def test_registry_urls():
    assert rp.registry_url("sdk-python", "0.1.3") == "https://pypi.org/pypi/commitlayer/0.1.3/json"
    assert rp.registry_url("recorder-js", "0.1.4") == "https://registry.npmjs.org/@commitlayer/mcp-recorder/0.1.4"
    assert (
        rp.registry_url("integrations-python", "1.0.0") == "https://pypi.org/pypi/commitlayer-integrations/1.0.0/json"
    )


def test_nothing_published_yet_plans_all_six_sdks_first(repo):
    result = rp.plan(fetch=Registry(), root=repo)
    assert result["to_publish"] == list(rp.ORDER)
    assert result["to_publish"][:2] == ["sdk-python", "sdk-js"]
    assert all(r["tag"] == f"{r['package']}-v{r['version']}" for r in result["packages"])


def test_published_versions_are_skipped_so_a_rerun_is_a_no_op(repo):
    v = _versions(repo)
    have = {rp.registry_url(n, v[n]) for n in rp.PACKAGES}
    result = rp.plan(fetch=Registry(have), root=repo)
    assert result["to_publish"] == []
    assert "nothing to do" in rp.summary(result)
    out = rp.github_output(result)
    assert "any=false" in out and "publish_sdk_python=false" in out


def test_only_the_ahead_packages_are_planned(repo):
    v = _versions(repo)
    have = {rp.registry_url(n, v[n]) for n in ("sdk-python", "recorder-js")}
    result = rp.plan(fetch=Registry(have), root=repo)
    assert result["to_publish"] == ["sdk-js", "recorder-python", "integrations-python", "integrations-js"]
    lines = dict(line.split("=", 1) for line in rp.github_output(result).splitlines())
    assert lines["publish_sdk_python"] == "false" and lines["publish_sdk_js"] == "true"
    assert lines["version_integrations_js"] == v["integrations-js"]
    assert json.loads(lines["packages"]) == result["to_publish"]


def test_a_tag_plans_only_its_package_and_must_match_the_manifest(repo):
    v = _versions(repo)["sdk-js"]
    result = rp.plan(tag=f"sdk-js-v{v}", fetch=Registry(), root=repo)
    assert result["to_publish"] == ["sdk-js"] and len(result["packages"]) == 1
    with pytest.raises(SystemExit, match="!= tag"):
        rp.plan(tag="sdk-js-v9.9.9", fetch=Registry(), root=repo)
    with pytest.raises(SystemExit, match="unknown package"):
        rp.plan(tag="nope-v1.0.0", fetch=Registry(), root=repo)


def test_a_tag_for_a_version_already_published_is_a_no_op(repo):
    v = _versions(repo)["recorder-python"]
    result = rp.plan(tag=f"recorder-python-v{v}", fetch=Registry({rp.registry_url("recorder-python", v)}), root=repo)
    assert result["to_publish"] == []


def test_only_restricts_and_rejects_unknown_names(repo):
    assert rp.plan(only=["integrations-js", "sdk-js"], fetch=Registry(), root=repo)["to_publish"] == [
        "sdk-js",
        "integrations-js",
    ]
    with pytest.raises(SystemExit, match="unknown packages"):
        rp.plan(only=["sdk-go"], fetch=Registry(), root=repo)


def test_a_registry_error_fails_the_plan_rather_than_guess(repo):
    v = _versions(repo)["sdk-python"]
    with pytest.raises(rp.RegistryError, match="503"):
        rp.plan(fetch=Registry(status={rp.registry_url("sdk-python", v): 503}), root=repo)


def test_pypi_is_downloadable_only_once_the_simple_index_lists_it():
    url = rp.registry_url("integrations-python", "0.2.0")
    simple = "https://pypi.org/simple/commitlayer-integrations/"
    assert not rp.downloadable("integrations-python", "0.2.0", Registry({url}))
    assert rp.downloadable(
        "integrations-python",
        "0.2.0",
        Registry({url}, simple={simple: "commitlayer_integrations-0.2.0-py3-none-any.whl"}),
    )
    assert rp.downloadable("sdk-js", "0.2.0", Registry({rp.registry_url("sdk-js", "0.2.0")}))


def test_wait_polls_until_downloadable_or_times_out():
    url = rp.registry_url("sdk-js", "0.2.0")
    reg = Registry()
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 30:
            reg.have.add(url)

    assert rp.wait("sdk-js", "0.2.0", timeout=120, interval=10, fetch=reg, sleep=sleep, clock=lambda: clock["t"])
    assert len(reg.calls) == 4
    clock["t"] = 0.0
    assert not rp.wait(
        "sdk-js", "9.9.9", timeout=25, interval=10, fetch=Registry(), sleep=sleep, clock=lambda: clock["t"]
    )


def test_wait_retries_through_a_registry_error():
    url = rp.registry_url("sdk-js", "0.2.0")
    reg = Registry(status={url: 502})
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
        reg.status.clear()
        reg.have.add(url)

    assert rp.wait("sdk-js", "0.2.0", timeout=60, interval=5, fetch=reg, sleep=sleep, clock=lambda: clock["t"])


def test_the_publish_order_matches_the_package_list():
    assert sorted(rp.ORDER) == sorted(rp.PACKAGES)
