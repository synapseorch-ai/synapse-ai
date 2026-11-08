"""
A packaged install keeps its data under SYNAPSE_HOME; a checkout keeps backend/var.

The package manager owns the directory a packaged install's code runs from, and
replaces it on upgrade — `npm i -g synapse-orch-ai@latest` deleted the database
that sat under it. So the launcher sets SYNAPSE_HOME and every default the
engine writes to follows it: the database, blobs, state, scratch and the .env
holding the generated secrets. The explicit per-thing variables still win, and
a checkout with nothing set is exactly where it was.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def bare(monkeypatch):
    """No per-thing override set, so each default is what is under test."""
    for var in ("SYNAPSE_HOME", "SYNAPSE_DB_URL", "SYNAPSE_BLOB_DIR",
                "SYNAPSE_STATE_DIR", "SYNAPSE_SCRATCH_DIR"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_a_checkout_keeps_backend_var_and_the_root_env(bare):
    from core.paths import env_file, var_root

    assert var_root() == BACKEND / "var"
    assert env_file() == BACKEND.parent / ".env"


def test_synapse_home_moves_var_and_env(bare, tmp_path):
    from core.paths import env_file, var_root

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    assert var_root() == tmp_path / "home" / "var"
    assert env_file() == tmp_path / "home" / ".env"


def test_a_tilde_in_synapse_home_is_expanded(bare, tmp_path):
    from core.paths import var_root

    bare.setenv("HOME", str(tmp_path))
    bare.setenv("USERPROFILE", str(tmp_path))
    bare.setenv("SYNAPSE_HOME", "~/.synapse")
    assert var_root() == tmp_path / ".synapse" / "var"


def test_the_database_follows_synapse_home(bare, tmp_path):
    from core.store.engine import default_url

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    assert default_url() == f"sqlite+aiosqlite:///{tmp_path / 'home' / 'var' / 'synapse.db'}"
    assert (tmp_path / "home" / "var").is_dir()


def test_synapse_db_url_still_wins(bare, tmp_path):
    from core.store.engine import default_url

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    bare.setenv("SYNAPSE_DB_URL", "postgresql://u:p@db/synapse")
    assert default_url() == "postgresql+asyncpg://u:p@db/synapse"


def test_blobs_follow_synapse_home_unless_named(bare, tmp_path):
    from core.storage.base import LocalBlobStore

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    assert LocalBlobStore().root == tmp_path / "home" / "var" / "blobs"
    bare.setenv("SYNAPSE_BLOB_DIR", str(tmp_path / "elsewhere"))
    assert LocalBlobStore().root == tmp_path / "elsewhere"


def test_state_follows_synapse_home_unless_named(bare, tmp_path):
    from core.runtime_dirs import state_root

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    assert state_root() == tmp_path / "home" / "var" / "state"
    bare.setenv("SYNAPSE_STATE_DIR", str(tmp_path / "idx"))
    assert state_root() == tmp_path / "idx"


def test_scratch_without_a_filesystem_store_falls_back_under_synapse_home(bare, tmp_path):
    import core.storage.scratch as scratch

    class _NoPathStore:
        def path_for(self, key):
            return None

    bare.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    bare.setattr("core.storage.get_blob_store", lambda: _NoPathStore())
    assert scratch.scratch_root() == tmp_path / "home" / "var" / "scratch" / "default"


def test_the_jwt_secret_is_read_from_synapse_homes_env(bare, tmp_path):
    from core.config import get_or_create_jwt_secret

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("SYNAPSE_JWT_SECRET=from-home\n", encoding="utf-8")
    bare.setenv("SYNAPSE_HOME", str(home))
    bare.delenv("SYNAPSE_JWT_SECRET", raising=False)
    assert get_or_create_jwt_secret() == "from-home"


def test_main_loads_synapse_homes_env_and_real_env_wins(tmp_path):
    """backend/main.py reads $SYNAPSE_HOME/.env before the server imports.

    Run in a child: loading a .env mutates os.environ, and the file must be
    read as a script (that is how every launcher runs it) without starting
    uvicorn, so the child stops at the import that follows the load.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "SYNAPSE_BACKEND_PORT=1234\nSYNAPSE_LOG_LEVEL=DEBUG\n", encoding="utf-8"
    )
    probe = (
        "import runpy, sys, os\n"
        "sys.modules['uvicorn'] = None\n"  # the import after the load raises
        "try:\n"
        f"    runpy.run_path({str(BACKEND / 'main.py')!r}, run_name='not_main')\n"
        "except ImportError:\n"
        "    pass\n"
        "print(os.environ.get('SYNAPSE_BACKEND_PORT'), os.environ.get('SYNAPSE_LOG_LEVEL'))\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SYNAPSE_HOME": str(home),
        "SYNAPSE_LOG_LEVEL": "WARNING",
        "PYTHONPATH": str(BACKEND),
    }
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1234", "WARNING"]
