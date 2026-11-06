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


def _npm_step(jobs, tmp_path, has: str) -> list[str]:
    """The npm step's own script, run with a stand-in `node` (package.json says 2.0.0-rc1) and `npm` (the registry has
    `has`, or nothing); the gate's npm_tag is `next`. Returns each `npm publish` it made."""
    import subprocess

    run = _step(jobs["publish-npm"], run="npm publish")["run"]
    assert run.count("${{ needs.gate.outputs.npm_tag }}") == 1
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "node").write_text("#!/bin/sh\necho 2.0.0-rc1\n")
    (bin_ / "npm").write_text('#!/bin/sh\ncase "$1" in\n  view) [ -n "$HAS" ] && echo "$HAS"; exit 0;;\n'
                              '  publish) echo "$*" >> "$LOG";;\nesac\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    log = tmp_path / "published"
    log.touch()
    subprocess.run(["bash", "-e", "-c", run.replace("${{ needs.gate.outputs.npm_tag }}", "next")], check=True,
                   env={"PATH": f"{bin_}:/usr/bin:/bin", "HAS": has, "LOG": str(log)})
    return log.read_text().splitlines()


def test_npm_publishes_under_the_scripts_tag(jobs, tmp_path):
    assert _npm_step(jobs, tmp_path, has="") == ["publish --access public --tag next"]


def test_npm_leaves_a_version_it_already_has(jobs, tmp_path):
    """npm refuses to publish over a version (E403). v2.0.0-rc1's tag was pushed again after its PyPI check failed,
    with npm's 2.0.0-rc1 already out: publishing again failed the job, and with it the GitHub release."""
    assert _npm_step(jobs, tmp_path, has="2.0.0-rc1") == []


def test_the_wheel_is_checked_with_its_dependencies(jobs):
    """2.0's CLI imports rich before it prints --help; a --no-deps install stopped v2.0.0-rc1's PyPI publish."""
    run = _step(jobs["publish-python"], run="synapse --help")["run"]
    installs = [line for line in run.splitlines() if "pip install" in line and ".whl" in line]
    assert installs and not [line for line in installs if "--no-deps" in line]


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


def _stamped(jobs, tmp_path, prerelease: str, tag: str) -> dict[str, str]:
    """The publish step's own stamp, run on copies of both setup scripts as the runner's checkout holds them."""
    import re
    import subprocess

    run = _step(jobs["github-release"], run="gh release create")["run"]
    block = re.search(r'^\s*if \[ "\$PRERELEASE" = "true" \]; then\n(\s*sed -i .*setup\.sh setup\.ps1\n)\s*fi$', run, re.M)
    assert block, "no pre-release stamp of setup.sh and setup.ps1 in the publish step"
    for name in ("setup.sh", "setup.ps1"):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    script = f'if [ "$PRERELEASE" = "true" ]; then\n{block.group(1)}fi\n'
    subprocess.run(["bash", "-c", script], cwd=tmp_path, check=True,
                   env={"PATH": "/usr/bin:/bin", "PRERELEASE": prerelease, "GITHUB_REF_NAME": tag})
    return {name: (tmp_path / name).read_text(encoding="utf-8") for name in ("setup.sh", "setup.ps1")}


def test_a_prereleases_setup_scripts_fetch_its_own_installer(jobs, tmp_path):
    """Both scripts fetch synapse-install.pyz (and its .sha256) from releases/latest, which a pre-release never
    becomes: v2.0.0-rc1's `curl …/releases/download/v2.0.0-rc1/setup.sh | bash` reached v1.9.3's release, which has
    no installer, and stopped at "Could not download the installer". The copies the release carries are stamped with
    their own release; the repo's files stay as they are."""
    stamped = _stamped(jobs, tmp_path, "true", "v2.0.0-rc1")
    for name, text in stamped.items():
        assert "releases/latest" not in text, name
        assert "https://github.com/synapseorch-ai/synapse-ai/releases/download/v2.0.0-rc1" in text, name
    assert "releases/latest/download" in (ROOT / "setup.sh").read_text(encoding="utf-8")


def test_a_final_releases_setup_scripts_are_carried_as_they_are(jobs, tmp_path):
    stamped = _stamped(jobs, tmp_path, "false", "v2.0.0")
    for name, text in stamped.items():
        assert text == (ROOT / name).read_text(encoding="utf-8"), name
