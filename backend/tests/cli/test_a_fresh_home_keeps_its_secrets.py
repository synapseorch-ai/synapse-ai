"""
A fresh pip install's first `synapse start` keeps its three secrets (F1442).

A packaged install keeps its generated secrets in SYNAPSE_HOME/.env
(~/.synapse/.env), and `synapse start` writes them before anything has made
that folder: on a fresh `pip install synapse-orch-ai`, each write failed with
"No such file or directory" and the secrets lived in that server's memory only.
`synapse access-link` then made a new token the running Synapse didn't know, so
its link didn't open Synapse, and the next restart made a new internal token
and sign-in secret, signing everyone out (v2.0.0-rc1's pip install test). The
installer and npm make ~/.synapse first, so only pip met it.
"""
from __future__ import annotations

import os

import pytest

cli = pytest.importorskip("synapse.cli")

SECRETS = ("SYNAPSE_INTERNAL_TOKEN", "SYNAPSE_JWT_SECRET", "SYNAPSE_ACCESS_TOKEN")


def _first_start(monkeypatch, home) -> dict[str, str]:
    """The three writers in the order `synapse start` runs them, with none of the secrets set yet."""
    monkeypatch.setenv("SYNAPSE_HOME", str(home))
    for var in SECRETS:  # set, then removed: monkeypatch only puts back a variable it saw, and the writers set all three
        monkeypatch.setenv(var, "unset")
        monkeypatch.delenv(var)
    cli._ensure_internal_token()
    cli._ensure_jwt_secret()
    cli._ensure_access_token()
    return {var: os.environ[var] for var in SECRETS}


def test_the_first_start_keeps_all_three_in_a_home_not_yet_made(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    made = _first_start(monkeypatch, home)
    assert "could not write" not in capsys.readouterr().out
    kept = (home / ".env").read_text(encoding="utf-8").splitlines()
    for var, value in made.items():
        assert f"{var}={value}" in kept, var


def test_a_later_command_reads_the_same_three(monkeypatch, tmp_path):
    """`synapse access-link`, or the next start, runs in a new process: it reads what the first start kept."""
    home = tmp_path / "home"
    made = _first_start(monkeypatch, home)
    link = cli._access_link(18894)
    for var in SECRETS:
        monkeypatch.delenv(var)
    assert _first_start(monkeypatch, home) == made
    assert cli._access_link(18894) == link
