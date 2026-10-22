"""
A pre-release tag publishes as a pre-release everywhere, and a final one as before (F827).

publish.yml treated every `v*` tag as a final release: npm `latest`, Docker
`:latest` and GitHub's releases/latest — which every 1.x `synapse upgrade` and
both setup scripts follow — so an rc would have been handed to every 1.x user.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("release_version", ROOT / "scripts" / "release_version.py")
release_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_version)


def _manifests(py: str, npm: str) -> tuple[str, str]:
    return f'[project]\nname = "synapse-orch-ai"\nversion = "{py}"\n', json.dumps({"version": npm})


def test_a_final_release_is_latest_everywhere():
    out = release_version.resolve("v2.0.0", *_manifests("2.0.0", "2.0.0"))
    assert out == {"pep440": "2.0.0", "npm": "2.0.0", "docker": "2.0.0",
                   "prerelease": "false", "npm_tag": "latest"}


@pytest.mark.parametrize("tag", ["v2.0.0-rc1", "v2.0.0rc1", "2.0.0-rc.1"])
def test_an_rc_is_a_prerelease_in_each_registrys_spelling(tag):
    out = release_version.resolve(tag, *_manifests("2.0.0rc1", "2.0.0-rc1"))
    assert out == {"pep440": "2.0.0rc1", "npm": "2.0.0-rc1", "docker": "2.0.0-rc1",
                   "prerelease": "true", "npm_tag": "next"}


def test_a_tag_naming_another_version_stops_the_release():
    with pytest.raises(release_version.Mismatch, match="pyproject.toml says 2.0.0rc1"):
        release_version.resolve("v2.0.0-rc2", *_manifests("2.0.0rc1", "2.0.0-rc1"))


def test_package_json_must_use_npms_spelling():
    with pytest.raises(release_version.Mismatch, match="npm spells 2.0.0rc1 as 2.0.0-rc1"):
        release_version.resolve("v2.0.0-rc1", *_manifests("2.0.0rc1", "2.0.0rc1"))


def test_the_script_prints_github_outputs(capsys, monkeypatch):
    monkeypatch.setattr(release_version, "ROOT", ROOT)
    assert release_version.main([f"v{json.loads((ROOT / 'package.json').read_text())['version']}"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert {line.split("=", 1)[0] for line in lines} == {"pep440", "npm", "docker", "prerelease", "npm_tag"}


# ── the workflow uses what the script decides ────────────────────────────────

@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "publish.yml").read_text())["jobs"]


def _step(job, *, run=None, uses=None):
    for step in job["steps"]:
        if run and run in str(step.get("run", "")):
            return step
        if uses and str(step.get("uses", "")).startswith(uses):
            return step
    raise AssertionError(f"no step with {run or uses}")


def test_the_gate_checks_the_version_with_the_script_and_passes_on_its_answers(jobs):
    step = _step(jobs["gate"], run="scripts/release_version.py")
    assert step["id"] == "rv"
    assert jobs["gate"]["outputs"] == {
        k: f"${{{{ steps.rv.outputs.{k} }}}}" for k in ("pep440", "npm", "docker", "prerelease", "npm_tag")
    }


def test_npm_publishes_under_the_scripts_tag(jobs):
    step = _step(jobs["publish-npm"], run="npm publish")
    assert step["run"].strip() == "npm publish --access public --tag ${{ needs.gate.outputs.npm_tag }}"


def test_docker_tags_latest_only_for_a_final_release(jobs):
    assert "gate" in jobs["publish-docker"]["needs"]
    tags = [t for t in _step(jobs["publish-docker"], uses="docker/build-push-action")["with"]["tags"].splitlines() if t.strip()]
    assert tags == [
        "${{ secrets.DOCKERHUB_USERNAME }}/${{ matrix.image }}:${{ needs.gate.outputs.docker }}",
        "${{ needs.gate.outputs.prerelease != 'true' && format('{0}/{1}:latest', secrets.DOCKERHUB_USERNAME, matrix.image) || '' }}",
    ]


def test_the_github_release_is_a_prerelease_for_an_rc_and_takes_the_notes_file(jobs):
    job = jobs["github-release"]
    assert "gate" in job["needs"]
    step = _step(job, run="gh release create")
    assert step["env"]["PRERELEASE"] == "${{ needs.gate.outputs.prerelease }}"
    run = step["run"]
    assert 'if [ "$PRERELEASE" = "true" ]; then kind=--prerelease; else kind=--latest; fi' in run
    assert "--latest" not in run.replace("else kind=--latest", "")
    assert 'notes=".github/release-notes/$GITHUB_REF_NAME.md"' in run


def test_pypi_and_the_installer_use_pypis_spelling(jobs):
    job = jobs["github-release"]
    assert "pypi.org/pypi/synapse-orch-ai/${{ needs.gate.outputs.pep440 }}/json" in _step(job, run="pypi.org")["run"]
    assert '--version "${{ needs.gate.outputs.pep440 }}"' in _step(job, run="build_installer.py")["run"]


def test_a_hung_image_build_fails_within_ninety_minutes(jobs):
    """F828: v1.9.2's combined image sat in "Build and push" for 4 h under GitHub's
    default 6-hour limit (1.9.1's took 26 min, the re-run 28)."""
    assert jobs["publish-docker"].get("timeout-minutes") == 90
