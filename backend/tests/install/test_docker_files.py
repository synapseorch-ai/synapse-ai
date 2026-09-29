"""
The same `docker run` keeps its data across the 1.x → 2.0 upgrade.

1.x's image and README mounted one volume at /data (`-v synapse-data:/data`) and
kept its JSON there; compose bind-mounted the host's ./backend/data. 2.0 had
moved the image's volume to /blobs — so an unchanged `docker run` put the new
database in an anonymous volume, lost on the next container, and the importer
never looked in /data. These pin the layout that makes the upgrade need no edit.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _env(dockerfile: str) -> dict:
    text = (REPO / dockerfile).read_text(encoding="utf-8")
    return dict(re.findall(r"^ENV (\w+)=(\S*)", text, re.M))


def _volumes(dockerfile: str) -> list[str]:
    return re.findall(r"^VOLUME (\S+)", (REPO / dockerfile).read_text(encoding="utf-8"), re.M)


def test_the_images_keep_everything_on_the_1x_volume():
    for dockerfile in ("Dockerfile", "Dockerfile.backend"):
        env = _env(dockerfile)
        assert _volumes(dockerfile) == ["/data"], dockerfile
        assert env["SYNAPSE_DB_URL"] == "sqlite+aiosqlite:////data/synapse.db", dockerfile
        assert env["SYNAPSE_BLOB_DIR"] == "/data/blobs", dockerfile
        # 1.x kept its internal token here; reusing it keeps sessions working.
        assert env["SYNAPSE_SECRETS_DIR"] == "/data", dockerfile
        # And the importer looks for the 1.x JSON on the volume.
        assert env["SYNAPSE_DATA_DIR"] == "/data", dockerfile
        # Rebuildable, per-replica: not on the volume.
        assert not env["SYNAPSE_STATE_DIR"].startswith("/data"), dockerfile


def test_the_entrypoint_defaults_to_the_volume():
    text = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert ': "${SYNAPSE_SECRETS_DIR:=/data}"' in text


def test_compose_reads_the_host_folder_1x_used():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    backend = text.split("\n  frontend:")[0]
    assert "- ./backend/data:/legacy-data" in backend
    assert "- SYNAPSE_DATA_DIR=/legacy-data" in backend


async def test_a_mounted_legacy_folder_is_retired_inside_itself(tmp_path, monkeypatch):
    """A mount point cannot be renamed; the consumed files move into it instead."""
    import json

    from core.store import importer

    mount = tmp_path / "legacy-data"
    (mount / "vault").mkdir(parents=True)
    (mount / "user_agents.json").write_text(json.dumps([{"id": "agent_m", "name": "M"}]))
    (mount / "vault" / "note.md").write_text("n")
    real_rename = Path.rename

    def refuse_the_mount(self, target):
        if self == mount:
            raise OSError(16, "Device or resource busy")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_the_mount)
    monkeypatch.setenv("SYNAPSE_DATA_DIR", str(mount))
    monkeypatch.setattr(importer, "_code_data_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    counts = await importer.import_legacy_data_if_present()

    assert counts["user_agents"] == 1 and counts["vault"] == 1
    assert (mount / "migrated" / "user_agents.json").is_file()
    assert (mount / "migrated" / "vault" / "note.md").is_file()
    assert await importer.import_legacy_data_if_present() is None
