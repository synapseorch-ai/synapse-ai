"""
Where this install keeps what it owns: one answer, read at call time.

A packaged install (pip, uv, npm) runs its code from a directory the package
manager owns and replaces on every upgrade — site-packages, a uv tool venv, the
npm global folder. Data under the code dies with the next upgrade, which is how
`npm update` came to delete the database. So the launcher sets ``SYNAPSE_HOME``
(``~/.synapse`` by default) and everything the install writes lands under it.

A source checkout sets nothing and keeps ``backend/var``, which is where a
developer's store already lives.

The finer knobs — ``SYNAPSE_DB_URL``, ``SYNAPSE_BLOB_DIR``, ``SYNAPSE_STATE_DIR``,
``SYNAPSE_SCRATCH_DIR`` — still win where they are read; this only moves the
default they fall back to. Read at call time rather than bound at import, so a
test or a launcher that sets the variable after ``core`` is imported is obeyed.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The directory holding `core/`: `backend/` in a checkout or a wheel, `/app` in
#: the backend image. Paths hang off it exactly as they did before this module,
#: so no layout moves.
_BACKEND_DIR = Path(__file__).resolve().parent.parent


def synapse_home() -> Path | None:
    """``SYNAPSE_HOME`` as a path, or None for a source checkout."""
    home = (os.getenv("SYNAPSE_HOME") or "").strip()
    return Path(home).expanduser() if home else None


def var_root() -> Path:
    """The directory holding the database, blobs, state and scratch."""
    home = synapse_home()
    return home / "var" if home else _BACKEND_DIR / "var"


def env_file() -> Path:
    """The ``.env`` the launcher writes this install's generated secrets to."""
    home = synapse_home()
    return home / ".env" if home else _BACKEND_DIR.parent / ".env"
