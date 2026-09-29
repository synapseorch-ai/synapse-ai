"""
A v1.x script install hands itself over to 2.0.

v1.x's `synapse upgrade` downloads the newest release's source over its own
folder and re-runs `synapse/cli.py upgrade` with the old venv's Python
(v1.9.1 cli.py:1199-1208). That puts this code — 2.0's — in charge, inside an
install layout 2.0 no longer has. `cli.main()` notices (`_legacy_install_root`)
before it imports anything the old venv might lack, and calls `run()`, which
installs 2.0 the new way, moves the data, and removes the old folder.

It runs for any command, not only `upgrade`: a hand-off interrupted half way
finishes the next time the user types `synapse` at all.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _clean_environment(root: Path) -> None:
    """Forget the old install: its venv, its data folder, its upgrade flag."""
    for var in ("SYNAPSE_UPGRADE_SKIP_DOWNLOAD", "SYNAPSE_DATA_DIR", "VIRTUAL_ENV",
                "PYTHONPATH", "PYTHONHOME"):
        os.environ.pop(var, None)
    venv = str(root / "backend" / "venv")
    os.environ["PATH"] = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not os.path.realpath(p).startswith(os.path.realpath(venv))
    )


def run(root: Path, argv: list[str]) -> int:
    _clean_environment(root)
    from .flow import Installer, Options

    command = argv[0] if argv else "start"
    code = Installer(Options(legacy_root=str(root),
                             no_start=command not in ("start", "upgrade", "restart"))).run()
    if code != 0 or command in ("start", "upgrade", "restart"):
        return code
    # Anything else — `synapse status`, `synapse agents` — runs now on 2.0.
    from .home import home_dir, read_install

    python = read_install(home_dir()).get("tool_python")
    if not python:
        return code
    # -P: this may run from inside the old folder, whose `synapse/` must not
    # stand in for the installed one. The user's directory is kept otherwise.
    return subprocess.run([python, "-P", "-m", "synapse", *argv]).returncode
