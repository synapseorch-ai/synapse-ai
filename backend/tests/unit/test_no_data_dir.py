"""
`DATA_DIR` is gone, and it stays gone.

One constant used to name the place settings, orchestrations, agents, tools,
schedules, usage, chat history, the vault, cached responses, run state, logs and
ChromaDB's index all lived — a single directory meaning four different kinds of
thing. That is what made the engine something you installed on a laptop rather
than something a request can be served by: a process could only ever hold one
install's worth of state, so it could only ever serve one tenant.

Removing it is easy to undo by accident. A new module needing somewhere to write
finds `os.getenv("SYNAPSE_DATA_DIR")` in the git history, or a helpful
`DATA_DIR = Path(__file__).parent.parent / "data"`, and the whole shape comes
back one module at a time — exactly how the five stale vault paths in
`test_vault_paths.py` survived a refactor that was supposed to remove them.

So these tests assert the absence, in the same spirit as
`test_no_env_secrets.py`. State now belongs to one of three layers, and which
one it is decides where the code goes:

    core/store/        a document with an identity      → a database row
    core/storage/      tenant content, file-shaped      → a blob key
    core/runtime_dirs  a genuine directory on disk      → a real path

There are exactly two exceptions, and they are the same job from both ends:
`core/store/importer.py` still knows where a pre-database install kept its
JSON, because migrating that folder away is the whole of its job; and
`synapse/installer/legacy.py` finds that install before the engine exists — it
reads the 1.x `.env`'s data folder to name it to the importer, and keeps the
variable out of every 2.0 process it starts. They are the only modules allowed
to say the name.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_REPO = _BACKEND.parent

#: The modules whose job is the legacy folder: the importer, and the installer's
#: side of the same migration (F304 — it runs before any engine module can be
#: imported, so it cannot ask the importer). A third entry means the constant
#: is coming back — that is the conversation to have, not a line to add.
_ALLOWED = {"backend/core/store/importer.py", "synapse/installer/legacy.py"}

#: Directories worth scanning. Tests are excluded because several of them drive
#: the importer, and `tests/unit/test_importer.py` must be able to set the
#: variable to point at a fixture.
_SCANNED = ("backend/core", "backend/services", "backend/tools", "synapse", "bin")


def _sources():
    for directory in _SCANNED:
        root = _REPO / directory
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".js"):
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            yield path


def test_the_constant_is_not_importable():
    """`from core.config import DATA_DIR` must fail, not resolve."""
    import core.config as config

    for name in ("DATA_DIR", "SETTINGS_FILE", "CREDENTIALS_FILE", "TOKEN_FILE"):
        assert not hasattr(config, name), (
            f"core.config.{name} is back. Settings are rows in the store; "
            f"see core/store/settings.py."
        )


def test_loading_settings_does_not_read_a_file():
    """Without a provider the answer is the shipped defaults, not a file.

    A file read here is how one tenant's settings became every tenant's: the
    process answers from whatever is on its own disk, which is precisely what a
    shared fleet cannot do.
    """
    from core.config import default_settings, load_settings, set_settings_provider

    set_settings_provider(None)
    try:
        assert load_settings() == default_settings()
    finally:
        set_settings_provider(None)


@pytest.mark.parametrize("path", list(_sources()), ids=lambda p: str(p.relative_to(_REPO)))
def test_no_module_reads_the_data_dir(path):
    """Nothing outside the importer may name the variable or the constant."""
    relative = str(path.relative_to(_REPO))
    source = path.read_text(encoding="utf-8", errors="replace")

    for needle in ("SYNAPSE_DATA_DIR", "DATA_DIR"):
        if needle not in source:
            continue
        # Prose explaining why it went is fine and worth keeping; a line of code
        # that reaches for it is not. Comments and docstrings are dropped by
        # checking only lines that are not obviously narrative.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if needle in line and not _is_prose(line)
        ]
        if offenders:
            assert relative in _ALLOWED, (
                f"{relative} reads the old data directory:\n  "
                + "\n  ".join(offenders)
                + "\n\nState goes to core/store/ (documents), core/storage/ "
                "(tenant content) or core/runtime_dirs.py (real directories)."
            )


def _is_prose(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("*")
        or stripped.startswith('"""')
        or stripped.startswith("``")
        or not any(c in stripped for c in "=(")
    )


def test_no_module_builds_a_data_dir_from_its_own_location():
    """`Path(__file__).parent / "data"` is the same bug wearing a disguise.

    Six modules built the path this way, which meant the configured location
    never reached them at all — `SYNAPSE_DATA_DIR` could not relocate what they
    wrote, and the sandbox in the test suite could not contain it. The visible
    symptom was the suite slowly filling `backend/data` on developer machines.
    """
    import re

    pattern = re.compile(r'__file__.{0,120}?["\']data["\']', re.DOTALL)
    offenders = [
        relative
        for path in _sources()
        if (relative := str(path.relative_to(_REPO))) not in _ALLOWED
        and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, (
        "These build a data directory from their own location: " + ", ".join(offenders)
    )


def test_importing_the_engine_creates_no_data_directory(tmp_path):
    """The import-time `os.makedirs` is gone, and nothing replaced it.

    Run in a subprocess against a throwaway working directory, because the
    check is about what an import does — and by the time this test file has been
    collected, everything has already been imported once.
    """
    program = (
        "import pathlib, sys\n"
        "import core.server, core.config, core.mcp_client\n"
        "import core.store.importer, core.vault, core.runtime_dirs\n"
        f"created = pathlib.Path({str(_BACKEND)!r}) / 'data'\n"
        "sys.exit(1 if created.exists() else 0)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    env.pop("SYNAPSE_DATA_DIR", None)
    env["SYNAPSE_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'store.db'}"
    env["SYNAPSE_BLOB_DIR"] = str(tmp_path / "blobs")
    env["SYNAPSE_STATE_DIR"] = str(tmp_path / "state")

    result = subprocess.run(
        [sys.executable, "-c", program], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"importing the engine created backend/data\n{result.stderr}"
    )


def test_booting_a_tool_server_creates_no_data_directory(tmp_path):
    """The tool servers are separate processes, and one of them did this.

    `tools/sandbox.py` built its vault root from `__file__` and `mkdir`'d it at
    import, so every spawn of the `vault_sandbox` tool recreated
    `backend/data/vault` — including every run of this test suite. Its Docker
    mount was `.exists()`-guarded, so the wrong path raised nothing; the
    container just came up without `/data` and tool code silently stopped being
    able to read vaulted results.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_BACKEND)
    env.pop("SYNAPSE_DATA_DIR", None)
    env["SYNAPSE_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'store.db'}"
    env["SYNAPSE_BLOB_DIR"] = str(tmp_path / "blobs")

    # The two that reach for paths. Importing is the whole test — a tool server
    # module body runs before `stdio_server()` is ever awaited, which is exactly
    # where the `mkdir` was. `tests/install/test_mcp_tool_servers.py` covers the
    # rest of the registry by actually booting each one.
    program = (
        "import pathlib, sys\n"
        "import tools.sandbox, tools.bash\n"
        f"created = pathlib.Path({str(_BACKEND)!r}) / 'data'\n"
        "sys.exit(1 if created.exists() else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, (
        f"a native tool server recreated backend/data at import\n{result.stderr}"
    )
