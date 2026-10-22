#!/usr/bin/env python3
"""
What a release tag publishes, for .github/workflows/publish.yml (F827).

Checks the tag, pyproject.toml and package.json name one version — pyproject in
PyPI's spelling, package.json in npm's (`2.0.0rc1` / `2.0.0-rc1`) — and says
whether it is a pre-release. A pre-release goes to PyPI as one (pip skips it
unless asked), to npm under `next`, to Docker Hub without `:latest`, and to
GitHub as a pre-release, so releases/latest — which 1.x's `synapse upgrade`
and the setup scripts follow — stays on the last final release.

    python scripts/release_version.py v2.0.0-rc1 >> "$GITHUB_OUTPUT"
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synapse import versions  # noqa: E402


class Mismatch(ValueError):
    pass


def resolve(tag: str, pyproject_text: str, package_json_text: str) -> dict:
    py = tomllib.loads(pyproject_text)["project"]["version"]
    npm = json.loads(package_json_text)["version"]
    wanted = tag[1:] if tag.startswith("v") else tag
    if versions.pep440(wanted) != versions.pep440(py):
        raise Mismatch(f"the tag says {wanted}, pyproject.toml says {py}")
    if npm != versions.npm(py):
        raise Mismatch(f"package.json says {npm}; npm spells {py} as {versions.npm(py)}")
    pre = versions.is_prerelease(py)
    return {
        "pep440": versions.pep440(py),        # PyPI's name for it, and the installer's
        "npm": npm,
        "docker": versions.npm(py),           # a Docker tag cannot hold `+`; semver's form is safe
        "prerelease": "true" if pre else "false",
        "npm_tag": "next" if pre else "latest",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: release_version.py <tag or version>", file=sys.stderr)
        return 2
    try:
        out = resolve(argv[0], (ROOT / "pyproject.toml").read_text(), (ROOT / "package.json").read_text())
    except Mismatch as exc:
        print(f"::error::Version mismatch — {exc}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
