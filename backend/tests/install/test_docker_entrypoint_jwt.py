"""
A Docker install keeps its sign-in secret across restarts (F1430).

`SYNAPSE_JWT_SECRET` signs every session and the website widget's tokens. Only
`synapse start` keeps it (in `.env`); in the image `.env` is /app/.env, in the
image layer, and nothing writes it, so each container start made a new one in
memory and every restart signed everyone out. The entrypoint keeps it on the
volume beside the internal and access tokens: made once, read on every start,
and a secret set in the environment still wins.

These run the real docker/entrypoint.sh with `sh`, its command printing what it
exported, so no Docker is needed.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO / "docker" / "entrypoint.sh"


def _start(tmp_path: Path, **env: str) -> str:
    """One container start: the entrypoint, then a command that writes the SYNAPSE_JWT_SECRET it was given."""
    out = tmp_path / "exported"
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SYNAPSE_SECRETS_DIR": str(tmp_path / "secrets"),
            "OUT": str(out)}
    subprocess.run(["sh", str(ENTRYPOINT), "sh", "-c", 'printf %s "$SYNAPSE_JWT_SECRET" > "$OUT"'],
                   env={**base, **env}, check=True, capture_output=True, timeout=60)
    return out.read_text()


GENERATE = {"SYNAPSE_AUTOGEN_TOKEN": "1", "SYNAPSE_TOKEN_MODE": "generate"}


def test_the_first_start_makes_it_on_the_volume_readable_only_by_its_owner(tmp_path):
    exported = _start(tmp_path, **GENERATE)
    kept = tmp_path / "secrets" / "jwt_secret"
    assert re.fullmatch(r"[0-9a-f]{64}", exported)
    assert kept.read_text().strip() == exported
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert not list((tmp_path / "secrets").glob("jwt_secret.tmp.*"))


def test_every_later_start_reads_the_same_one(tmp_path):
    first = _start(tmp_path, **GENERATE)
    kept = (tmp_path / "secrets" / "jwt_secret").read_text()
    assert _start(tmp_path, **GENERATE) == first
    assert (tmp_path / "secrets" / "jwt_secret").read_text() == kept


def test_a_secret_set_in_the_environment_wins_and_nothing_is_written(tmp_path):
    assert _start(tmp_path, **GENERATE, SYNAPSE_JWT_SECRET="set-by-the-operator") == "set-by-the-operator"
    assert not (tmp_path / "secrets" / "jwt_secret").exists()


def test_the_frontends_read_mode_never_makes_it_and_takes_the_backends(tmp_path):
    """Compose's frontend shares /secrets in read mode: the backend's container is the one that makes it."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "internal_token").write_text("t" * 64)  # the backend's, so read mode does not wait for it
    read = {"SYNAPSE_AUTOGEN_TOKEN": "1", "SYNAPSE_TOKEN_MODE": "read"}
    assert _start(tmp_path, **read) == ""
    assert not (secrets / "jwt_secret").exists()
    (secrets / "jwt_secret").write_text("b" * 64)
    assert _start(tmp_path, **read) == "b" * 64


def test_nothing_is_made_when_the_tokens_are_not_generated(tmp_path):
    assert _start(tmp_path) == ""
    assert not (tmp_path / "secrets" / "jwt_secret").exists()
