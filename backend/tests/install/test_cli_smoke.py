"""
The CLI runs at all — driven through `main()`, as a user runs it.

`synapse api-keys generate|list|revoke` was broken for an entire release: when
`core.api_keys` went async, the CLI kept calling it synchronously, so `generate`
returned a coroutine and died on `record['name']`. Running the suite with
`-W error::RuntimeWarning` is what catches un-awaited coroutines everywhere
else, and it could not see this one for a simple reason — **the CLI had no
tests**, so nothing ever executed the line. A warning only fires on code that
runs.

These are deliberately end-to-end through `main()` rather than calls to the
`_*_command` helpers, because roughly half the surface worth protecting is in
argparse: `api-keys revoke <id>` and `api-keys generate <name>` share one
positional, and which one it becomes is decided by the dispatch in `main()`.

Sync tests on purpose. The CLI has no event loop of its own — `synapse/_store.py`
opens one per call with `asyncio.run`, which is safe precisely because the
process is short-lived and single-threaded, and which would raise inside an
already-running loop.
"""
import re
import sys

import pytest

cli = pytest.importorskip(
    "synapse.cli", reason="the synapse package is not importable in this environment"
)


@pytest.fixture
def clean_install(tmp_path, monkeypatch):
    """A CLI pointed at an empty database of its own.

    `get_store()` creates the schema on first use, so nothing needs migrating
    up front — touching the store is what brings the database into existence.
    """
    monkeypatch.setenv("SYNAPSE_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.delenv("SYNAPSE_DATA_DIR", raising=False)
    return tmp_path


def run_cli(monkeypatch, capsys, *argv):
    """Invoke `synapse <argv>` and return its stdout."""
    monkeypatch.setattr(sys, "argv", ["synapse", *argv])
    cli.main()
    return capsys.readouterr().out


# ── api keys ─────────────────────────────────────────────────────────────────

def test_api_keys_generate_then_list_then_revoke(clean_install, monkeypatch, capsys):
    """The whole lifecycle, in the order a user does it.

    The assertion that matters is `record['name']`: an un-awaited coroutine
    reaches this line as a truthy object and raises TypeError, which is exactly
    how this broke.
    """
    generated = run_cli(monkeypatch, capsys, "api-keys", "generate", "Smoke test key")
    assert "API Key generated successfully" in generated
    assert "Smoke test key" in generated

    key_id = re.search(r"ID:\s+(\S+)", generated)
    assert key_id, f"generate printed no key id:\n{generated}"

    listed = run_cli(monkeypatch, capsys, "api-keys", "list")
    assert "Smoke test key" in listed
    assert "Total: 1 key(s)" in listed

    revoked = run_cli(monkeypatch, capsys, "api-keys", "revoke", key_id.group(1))
    assert "deleted" in revoked

    assert "No API keys found" in run_cli(monkeypatch, capsys, "api-keys", "list")


def test_api_keys_list_on_a_fresh_install_says_so(clean_install, monkeypatch, capsys):
    """An empty install must print guidance, not an empty table or a traceback."""
    assert "No API keys found" in run_cli(monkeypatch, capsys, "api-keys", "list")


def test_revoking_an_unknown_key_exits_non_zero(clean_install, monkeypatch, capsys):
    """`synapse api-keys revoke bogus` is a script's error path — it must fail."""
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, capsys, "api-keys", "revoke", "no_such_key")
    assert exit_info.value.code != 0


# ── migrate ──────────────────────────────────────────────────────────────────

def test_migrate_on_an_empty_install_creates_the_schema(clean_install, monkeypatch, capsys):
    out = run_cli(monkeypatch, capsys, "migrate")

    assert "Database is up to date." in out
    assert "No pre-database install found to import." in out
    assert (clean_install / "store.db").is_file()


def test_migrate_imports_a_pre_database_install(clean_install, monkeypatch, capsys):
    """The upgrade path, which is the reason the command exists.

    `synapse migrate` is what a container entrypoint or a Kubernetes init
    container calls: it must do the schema work *and* bring a pre-database
    install across without the server ever starting.
    """
    import json

    legacy = clean_install / "data"
    legacy.mkdir()
    (legacy / "orchestrations.json").write_text(json.dumps([
        {"id": "orch_legacy1", "name": "Nightly report",
         "entry_step_id": "s1", "steps": [{"id": "s1", "type": "print"}]},
    ]), encoding="utf-8")
    (legacy / "settings.json").write_text(
        json.dumps({"agent_name": "Imported"}), encoding="utf-8"
    )
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(legacy))

    out = run_cli(monkeypatch, capsys, "migrate")

    assert "orchestrations=1" in out
    assert "settings=1" in out
    # The folder is retired, so a second run cannot double-import.
    assert not legacy.exists()
    assert (clean_install / "data.migrated").is_dir()

    assert "No pre-database install found to import." in run_cli(
        monkeypatch, capsys, "migrate"
    )


# ── version ──────────────────────────────────────────────────────────────────

class TestVersion:
    """`SECURITY.md` asks reporters for `synapse --version`, which did not exist,
    and the version the upgrade check read came from `pyproject.toml` — absent
    from a pip install, which therefore reported 0.0.0 (F80)."""

    def test_the_flag_prints_the_version(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["synapse", "--version"])
        with pytest.raises(SystemExit) as exited:
            cli.main()

        assert exited.value.code == 0
        assert capsys.readouterr().out.strip() == f"synapse {cli._get_current_version()}"

    def test_a_source_checkout_reads_pyproject(self):
        import tomllib
        pyproject = tomllib.loads((cli.ROOT_DIR / "pyproject.toml").read_text())
        assert cli._get_current_version() == pyproject["project"]["version"]

    def test_a_pip_install_reads_the_installed_package(self, monkeypatch, tmp_path):
        import importlib.metadata as metadata
        monkeypatch.setattr(cli, "ROOT_DIR", tmp_path)       # no pyproject.toml beside the package
        monkeypatch.setattr(metadata, "version", lambda dist: "9.8.7" if dist == "synapse-orch-ai" else None)

        assert cli._get_current_version() == "9.8.7"

    def test_the_npm_wrapper_does_not_start_the_server_for_it(self):
        source = (cli.ROOT_DIR / "bin" / "synapse.js").read_text()
        assert "cliArg === '--version'" in source


def test_profile_sends_the_internal_token(monkeypatch, capsys):
    """`synapse profile` called `/api/profiling/*` with no `X-Synapse-Internal`,
    so on any install with a token — every `synapse start` writes one — it was
    refused: measured as `Error 403: {"detail":"Forbidden"}` (F160; its commit says F158)."""
    import urllib.request

    sent = {}

    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        sent.update(req.headers)
        return _Resp()

    monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", "the-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    run_cli(monkeypatch, capsys, "profile", "stats")
    assert sent.get("X-synapse-internal") == "the-token"
