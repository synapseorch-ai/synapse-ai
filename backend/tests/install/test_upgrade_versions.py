"""
`synapse upgrade` orders versions the way PyPI does, pre-releases included (F826).

It read each dot part as an int and fell back to `(0,)`, so `2.0.0rc1` sorted
below every release: an rc install would "upgrade" down to the newest final on
PyPI (1.9.2, a data store it cannot read), and an rc target never compared above
anything. The rc is how 2.0.0 is tested before it ships (REL2, D312).
"""
from __future__ import annotations

import pytest

cli = pytest.importorskip("synapse.cli")


@pytest.mark.parametrize("older, newer", [
    ("1.9.2", "2.0.0rc1"),
    ("2.0.0.dev1", "2.0.0a1"),
    ("2.0.0a1", "2.0.0b1"),
    ("2.0.0b1", "2.0.0rc1"),
    ("2.0.0rc1", "2.0.0rc2"),
    ("2.0.0rc2", "2.0.0"),
    ("2.0.0", "2.0.0.post1"),
    ("2.0.0.post1", "2.0.1"),
    ("2.0.9", "2.0.10"),
    ("2.0.0rc1.dev2", "2.0.0rc1"),
])
def test_versions_sort_as_pypi_sorts_them(older, newer):
    assert cli._parse_version(older) < cli._parse_version(newer)


@pytest.mark.parametrize("a, b", [
    ("2.0.0", "v2.0.0"),
    ("2.0", "2.0.0"),
    ("2.0.0rc1", "2.0.0-rc1"),
    ("2.0.0rc1", "2.0.0-rc.1"),
    ("2.0.0rc1", "2.0.0.rc1"),
    ("2.0.0rc1", "2.0.0RC1"),
    ("2.0.0a1", "2.0.0alpha1"),
    ("2.0.0b1", "2.0.0-beta.1"),
    ("2.0.0.post1", "2.0.0-post1"),
])
def test_spellings_of_one_version_are_equal(a, b):
    assert cli._parse_version(a) == cli._parse_version(b)


def test_an_unreadable_version_sorts_lowest_and_never_raises():
    assert cli._parse_version("not-a-version") < cli._parse_version("0.0.1")
    assert cli._parse_version("") < cli._parse_version("1.0.0")


@pytest.mark.parametrize("pep440, semver", [
    ("2.0.0", "2.0.0"),
    ("2.0.0rc1", "2.0.0-rc1"),
    ("2.0.0a2", "2.0.0-alpha2"),
    ("2.0.0b3", "2.0.0-beta3"),
    ("2.0.0.post1", "2.0.0-post1"),
])
def test_the_npm_spelling_of_a_version(pep440, semver):
    assert cli._npm_version(pep440) == semver


# ── what `synapse upgrade` does with them ────────────────────────────────────

def _world(monkeypatch, *, current, latest, method="uv"):
    seen = []
    monkeypatch.setattr(cli, "_install_method", lambda: method)
    monkeypatch.setattr(cli, "_get_current_version", lambda: current)
    monkeypatch.setattr(cli, "_latest_version", lambda: latest)
    monkeypatch.setattr(cli, "_installed_extras", lambda: [])
    monkeypatch.setattr(cli, "_upgrade_with_installer", lambda v, **kw: seen.append(v))
    monkeypatch.setattr(cli, "_upgrade_with_pip", lambda cur, v, extras: seen.append(v) or 0)
    return seen


@pytest.mark.parametrize("method", ["uv", "pip"])
def test_an_rc_is_never_taken_down_to_the_last_final(monkeypatch, capsys, method):
    """PyPI's `info.version` is the newest final, 1.9.2, while an rc of 2.0 is installed."""
    seen = _world(monkeypatch, current="2.0.0rc1", latest="1.9.2", method=method)
    cli._upgrade_command()
    assert seen == []
    assert capsys.readouterr().out.strip() == "Synapse 2.0.0rc1 is up to date."


def test_an_rc_upgrades_to_its_final(monkeypatch):
    seen = _world(monkeypatch, current="2.0.0rc1", latest="2.0.0")
    cli._upgrade_command()
    assert seen == ["2.0.0"]


def test_a_named_rc_is_installed_when_asked_for(monkeypatch):
    seen = _world(monkeypatch, current="2.0.0rc1", latest="1.9.2")
    cli._upgrade_command(version="2.0.0rc2")
    assert seen == ["2.0.0rc2"]


def test_an_npm_install_is_told_the_npm_spelling(monkeypatch, capsys):
    """npm's versions are semver: `synapse-orch-ai@2.0.0rc2` names nothing there."""
    _world(monkeypatch, current="2.0.0rc1", latest="1.9.2", method="npm")
    cli._upgrade_command(version="2.0.0rc2")
    assert "npm install -g synapse-orch-ai@2.0.0-rc2" in capsys.readouterr().out
