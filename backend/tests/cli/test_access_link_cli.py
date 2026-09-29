"""The launcher's half of the access link (D124): made once, kept, rotatable."""
import pytest

from synapse import cli


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT_DIR", tmp_path)
    # A checkout: no SYNAPSE_HOME, so the .env is the one at ROOT_DIR. (The
    # suite sets one per test; a packaged install's .env is covered in
    # test_cli_packaged_mode.py.)
    monkeypatch.delenv("SYNAPSE_HOME", raising=False)
    # set, then delete: monkeypatch then restores the variable's absence after
    # the test — `_ensure_access_token` writes os.environ directly, and a token
    # left behind turns the gate on for every test that follows.
    monkeypatch.setenv("SYNAPSE_ACCESS_TOKEN", "placeholder")
    monkeypatch.delenv("SYNAPSE_ACCESS_TOKEN")
    (tmp_path / ".env").write_text("SYNAPSE_INTERNAL_TOKEN=keep-me\n")
    return tmp_path


def test_it_is_made_once_and_kept(root):
    first = cli._ensure_access_token()
    assert len(first) >= 40
    assert f"SYNAPSE_ACCESS_TOKEN={first}" in (root / ".env").read_text()
    # A new process reads the same one back rather than making another.
    import os
    os.environ.pop("SYNAPSE_ACCESS_TOKEN")
    assert cli._ensure_access_token() == first


def test_rotating_replaces_it_and_keeps_everything_else(root):
    first = cli._ensure_access_token()
    second = cli._ensure_access_token(rotate=True)
    env = (root / ".env").read_text()
    assert second != first
    assert env.count("SYNAPSE_ACCESS_TOKEN=") == 1 and f"SYNAPSE_ACCESS_TOKEN={second}" in env
    assert "SYNAPSE_INTERNAL_TOKEN=keep-me" in env


def test_the_link_carries_the_token(root):
    token = cli._ensure_access_token()
    assert cli._access_link(3000) == f"http://localhost:3000/?token={token}"
