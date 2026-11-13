"""
Templates ship with every install (F1568).

Workflows → Templates said "No example packs available." on every pip, installer and npm install (SHN1, 11-13, a
fresh 2.0.8): the route reads `backend/examples/index.json` beside the code (`core/routes/settings.py`), and the
wheel's `include` had `backend/core/`, `services/`, `tools/` and `main.py` but not `backend/examples/`. The installer
and npm install that same wheel. The two images that serve the app copied `core`, `services` and `tools` and not the
packs either. Home's "Build a workflow" card offers **Browse templates** as its first button, so a new person's first
press led to an empty page.

Hatchling is not part of the test environment, so the wheel's file set is read as hatchling reads it: the `include`
patterns of `[tool.hatch.build.targets.wheel]`, each a path relative to the repo, a directory one when it ends in `/`.
The packs are then laid out as the wheel installs them, away from the repo, and the route reads them there.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "backend" / "examples"


def _wheel_include() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]


def _in_wheel(path: str, include: list[str]) -> bool:
    for pattern in include:
        pattern = pattern.strip().lstrip("/")
        if pattern.endswith("/"):
            if path.startswith(pattern):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _wheel_files(include: list[str]) -> list[str]:
    """Every file of the repo the wheel's `include` takes, but bytecode."""
    found = []
    for pattern in include:
        here = REPO / pattern.strip().rstrip("/")
        if here.is_file():
            found.append(here.relative_to(REPO).as_posix())
        elif here.is_dir():
            for root, dirs, files in os.walk(here):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                found += [(Path(root) / f).relative_to(REPO).as_posix() for f in files if not f.endswith(".pyc")]
    return sorted(set(found))


def _packs() -> list[dict]:
    return json.loads((EXAMPLES / "index.json").read_text(encoding="utf-8"))


def test_the_examples_folder_is_the_packs_and_nothing_else():
    """What the wheel takes from it: the index and each pack it names. Nothing else lives there (no data, no
    secrets): the folder is shipped whole."""
    named = {"index.json"} | {pack["file"] for pack in _packs()}
    assert {p.name for p in EXAMPLES.iterdir()} == named
    assert len(_packs()) >= 5


def test_the_wheel_takes_every_pack_the_templates_page_reads():
    include = _wheel_include()
    wanted = ["backend/examples/index.json"] + [f"backend/examples/{pack['file']}" for pack in _packs()]
    missing = [path for path in wanted if not _in_wheel(path, include)]
    assert missing == [], f"the wheel leaves out {missing}"


def test_the_wheel_takes_no_install_data():
    include = _wheel_include()
    for path in ("backend/data/settings.json", "backend/var/synapse.db", ".env", "backend/.env",
                 "backend/venv/bin/python"):
        assert not _in_wheel(path, include), path


def test_an_installed_backend_finds_the_packs_where_the_wheel_puts_them(tmp_path):
    """The backend's files as the wheel lays them out (`site-packages/backend/…`), copied away from the repo; the
    route run from there lists every pack and opens each one."""
    site = tmp_path / "site-packages"
    for rel in _wheel_files(_wheel_include()):
        if rel.startswith("backend/"):
            (site / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, site / rel)

    code = """
import asyncio, json, sys
from core.routes import settings
assert settings.__file__.startswith(sys.argv[1]), settings.__file__
packs = asyncio.run(settings.get_examples())
opened = [p["id"] for p in packs if asyncio.run(settings.get_example_bundle(p["id"]))]
print("PACKS " + json.dumps({"listed": [p["id"] for p in packs], "opened": opened}))
"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(site / "backend")
    env["SYNAPSE_HOME"] = str(tmp_path / "home")
    out = subprocess.run([sys.executable, "-c", code, str(site)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=180)
    line = next((x for x in out.stdout.splitlines() if x.startswith("PACKS ")), None)
    assert line, out.stderr[-2000:]
    seen = json.loads(line.removeprefix("PACKS "))
    ids = [pack["id"] for pack in _packs()]
    assert seen == {"listed": ids, "opened": ids}


def test_the_images_that_serve_the_app_ship_the_packs():
    """Beside `core/`, where the route looks for them: `/app/backend/examples` in the all-in-one image,
    `/app/examples` in the backend image. The worker serves no pages."""
    for dockerfile in ("Dockerfile", "Dockerfile.backend"):
        text = (REPO / dockerfile).read_text(encoding="utf-8")
        assert re.search(r"^COPY backend/core \./core$", text, re.M), dockerfile
        assert re.search(r"^COPY backend/examples \./examples$", text, re.M), f"{dockerfile} leaves out the packs"
