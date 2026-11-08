"""Synapse's MCP endpoint reports Synapse's version, in the Docker image too — F1462 (ISS1, GitHub #378).

#378, 2.0.0-rc2's Docker image (arm64): `initialize` against `/api/v1/mcp` answered
`serverInfo: {"name": "synapse", "version": "1.29.0"}` — the pinned `mcp` SDK's version. `synapse_version()` read the
`synapse-orch-ai` package, else `parents[3]/pyproject.toml`; the image has no package, its module sits at
`/app/core/mcp_server/server.py` (so parents[3] is `/`), and the Dockerfiles copied only core, services, tools and
main.py. `Server(..., version=None)` then took the SDK's own.

The images that serve the API now carry `pyproject.toml` beside `core/`, and the version is read from the nearest
`pyproject.toml` above the module whose `[project]` is synapse-orch-ai — not a stranger's, and not the first
`version =` line of any file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVING = ("Dockerfile", "Dockerfile.backend")  # the images whose main.py serves /api/v1/mcp

OURS = '[project]\nname = "synapse-orch-ai"\nversion = "2.0.0"\n\n[[tool.uv.index]]\nname = "pytorch-cpu"\n'


def _module_at(root: Path, *parts: str) -> Path:
    here = root.joinpath(*parts, "core", "mcp_server", "server.py")
    here.parent.mkdir(parents=True)
    here.write_text("")
    return here


class TestWhereItLooks:
    def test_the_backend_images_layout(self, tmp_path):
        from core.mcp_server.server import version_beside

        here = _module_at(tmp_path / "app")
        (tmp_path / "app" / "pyproject.toml").write_text(OURS)
        assert version_beside(here) == "2.0.0"

    def test_the_combined_images_layout(self, tmp_path):
        from core.mcp_server.server import version_beside

        here = _module_at(tmp_path / "app", "backend")
        (tmp_path / "app" / "backend" / "pyproject.toml").write_text(OURS)
        assert version_beside(here) == "2.0.0"

    def test_a_source_checkout(self, tmp_path):
        from core.mcp_server.server import version_beside

        here = _module_at(tmp_path / "repo", "backend")
        (tmp_path / "repo" / "pyproject.toml").write_text(OURS)
        assert version_beside(here) == "2.0.0"

    def test_a_strangers_pyproject_is_not_read(self, tmp_path):
        from core.mcp_server.server import version_beside

        here = _module_at(tmp_path / "app")
        (tmp_path / "app" / "pyproject.toml").write_text('[project]\nname = "someone-else"\nversion = "9.9.9"\n')
        assert version_beside(here) is None

    def test_none_when_there_is_none(self, tmp_path):
        from core.mcp_server.server import version_beside

        assert version_beside(_module_at(tmp_path / "app")) is None


class TestTheVersionItReports:
    def test_with_no_package_installed_it_is_the_pyproject_s(self, monkeypatch):
        import importlib.metadata as md

        from core.mcp_server import server

        def not_installed(name):
            raise md.PackageNotFoundError(name)

        monkeypatch.setattr(md, "version", not_installed)
        import tomllib
        ours = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        assert server.synapse_version() == ours


class TestTheImages:
    @pytest.mark.parametrize("dockerfile", SERVING)
    def test_each_serving_image_copies_pyproject_beside_core(self, dockerfile):
        """`COPY pyproject.toml ./` under the same WORKDIR as `COPY backend/core ./core`: beside it, parents[2]."""
        workdir, core_in, pyproject_in = None, None, None
        for line in (ROOT / dockerfile).read_text().splitlines():
            if m := re.match(r"\s*WORKDIR\s+(\S+)", line):
                workdir = m.group(1)
            elif re.match(r"\s*COPY\s+backend/core\s+\./core\s*$", line):
                core_in = workdir
            elif re.match(r"\s*COPY\s+pyproject\.toml\s+\./(pyproject\.toml)?\s*$", line):
                pyproject_in = workdir
        assert core_in, f"{dockerfile} no longer copies backend/core to ./core"
        assert pyproject_in == core_in, (dockerfile, core_in, pyproject_in)
