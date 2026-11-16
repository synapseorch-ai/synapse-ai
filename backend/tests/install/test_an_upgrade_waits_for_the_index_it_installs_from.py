"""An upgrade waits for the package index it installs from, and changes nothing until that index serves the version
(F1638).

`synapse upgrade` took its target from PyPI's JSON page while uv and pip install from PyPI's simple index. The two are
cached apart, and for some minutes after a release they disagree: the upgrade announced the new version, stopped
Synapse, backed up the database, then failed with the resolver's words, leaving the stopped Synapse stopped. Now the
target is the newest final the install's own index lists (`synapse.installer.index`), versions compared as versions;
and before anything is stopped or backed up, an index that answered without the version is said plainly: still
reaching it, nothing changed, try again in a few minutes. uv re-reads the package's page, and pip skips its cache, so
neither installs from a stale copy of its own. An npm upgrade waits for the npm registry the same way.

Near misses, each as today: an index that does not answer in PEP 691 JSON (HTML only, a 404, a timeout) never blocks
the upgrade or says "still reaching"; PyPI unreachable keeps today's words; a pre-release is found by its exact
version; an index the person set (UV_DEFAULT_INDEX) is the one asked; and where the install could find the package
somewhere the check cannot see (find-links, an extra index, an index named in a uv.toml), the check stands aside.
"""
from __future__ import annotations

import pytest

cli = pytest.importorskip("synapse.cli")

PYPI_JSON = "https://pypi.org/pypi/synapse-orch-ai/json"
PYPI_SIMPLE = "https://pypi.org/simple/synapse-orch-ai/"


def _index():
    from synapse.installer import index

    return index


def _answer(monkeypatch, versions, *, asked=None):
    """The index answers in PEP 691 JSON with `versions` (None: it does not answer in that form). Before the fix there
    is no index module, and the upgrade goes on as today."""
    try:
        index = _index()
    except ImportError:
        return

    def read(url, headers=None, timeout=15):
        if asked is not None:
            asked.append(url)
        if versions is None:
            from synapse.installer.net import HttpError

            raise HttpError(f"{url}: not JSON")
        return {"meta": {"api-version": "1.1"}, "name": "synapse-orch-ai", "versions": list(versions), "files": []}

    monkeypatch.setattr(index, "_read", read)


def _pypi_json(monkeypatch, version):
    from synapse.installer import net

    real = net.get_json

    def get_json(url, headers=None, timeout=15, **kw):
        if url == PYPI_JSON:
            if version is None:
                raise net.HttpError(f"{url}: unreachable")
            return {"info": {"version": version}}
        return real(url, headers=headers, timeout=timeout, **kw)

    monkeypatch.setattr(net, "get_json", get_json)


def _world(monkeypatch, tmp_path, *, current="2.0.15", method="uv"):
    seen = []
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    for var in ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
                "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "UV_CONFIG_FILE", "PIP_CONFIG_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_install_method", lambda: method)
    monkeypatch.setattr(cli, "_get_current_version", lambda: current)
    monkeypatch.setattr(cli, "_installed_extras", lambda: [])
    monkeypatch.setattr(cli, "_upgrade_with_installer", lambda v, **kw: seen.append(v))
    monkeypatch.setattr(cli, "_upgrade_with_pip", lambda cur, v, extras: seen.append(v) or 0)
    return seen


# ── the target is the install's own index's newest ───────────────────────────

class TestTheTargetIsWhatTheInstallCanSee:
    def test_versions_compare_as_versions_never_as_text(self, monkeypatch):
        """2.0.9 sorts after 2.0.16 as text; as a version it is older."""
        _answer(monkeypatch, ["2.0.9", "2.0.16", "2.0.10", "2.1.0rc1"])
        assert _index().newest_final(["2.0.9", "2.0.16", "2.0.10", "2.1.0rc1"]) == "2.0.16"

    def test_the_index_the_install_reads_decides_not_the_json_page(self, monkeypatch, tmp_path):
        _world(monkeypatch, tmp_path)
        _pypi_json(monkeypatch, "2.0.16")
        _answer(monkeypatch, ["2.0.9", "2.0.14", "2.0.15"])
        assert cli._latest_version() == "2.0.15"

    def test_an_index_that_does_not_answer_in_json_leaves_the_target_as_today(self, monkeypatch, tmp_path):
        _world(monkeypatch, tmp_path)
        _pypi_json(monkeypatch, "2.0.16")
        _answer(monkeypatch, None)
        assert cli._latest_version() == "2.0.16"

    def test_pypi_unreachable_keeps_todays_words(self, monkeypatch, tmp_path, capsys):
        seen = _world(monkeypatch, tmp_path)
        _pypi_json(monkeypatch, None)
        _answer(monkeypatch, None)
        with pytest.raises(SystemExit):
            cli._upgrade_command()
        assert "Could not reach PyPI to look for a newer Synapse." in capsys.readouterr().out and seen == []


# ── nothing is stopped or backed up until the index serves the version ───────

class TestNothingChangesUntilTheIndexServesIt:
    @pytest.mark.parametrize("method", ["uv", "pip"])
    def test_a_version_the_index_lacks_is_said_plainly_and_nothing_runs(self, monkeypatch, tmp_path, capsys, method):
        seen = _world(monkeypatch, tmp_path, method=method)
        _answer(monkeypatch, ["2.0.14", "2.0.15"])
        with pytest.raises(SystemExit) as stopped:
            cli._upgrade_command(version="2.0.16")
        out = capsys.readouterr().out
        assert stopped.value.code == 1 and seen == [], out
        assert "Synapse 2.0.16 is still reaching PyPI" in out and "Nothing was changed" in out, out
        assert "try again in a few minutes" in out, out

    def test_the_index_serving_it_goes_on_as_before(self, monkeypatch, tmp_path):
        seen = _world(monkeypatch, tmp_path)
        _answer(monkeypatch, ["2.0.15", "2.0.16"])
        cli._upgrade_command(version="2.0.16")
        assert seen == ["2.0.16"]

    def test_an_index_that_does_not_answer_in_json_never_blocks_it(self, monkeypatch, tmp_path, capsys):
        seen = _world(monkeypatch, tmp_path)
        _answer(monkeypatch, None)
        cli._upgrade_command(version="2.0.16")
        assert seen == ["2.0.16"] and "still reaching" not in capsys.readouterr().out

    @pytest.mark.parametrize("asked_for", ["2.1.0rc1", "2.1.0-rc1"])
    def test_a_pre_release_is_found_by_its_exact_version(self, monkeypatch, tmp_path, asked_for):
        seen = _world(monkeypatch, tmp_path)
        _answer(monkeypatch, ["2.0.16", "2.1.0rc1"])
        cli._upgrade_command(version=asked_for)
        assert seen == [asked_for]

    def test_the_persons_own_index_is_the_one_asked(self, monkeypatch, tmp_path, capsys):
        _world(monkeypatch, tmp_path)
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://mirror.example.com/simple")
        asked: list[str] = []
        _answer(monkeypatch, ["2.0.15"], asked=asked)
        with pytest.raises(SystemExit):
            cli._upgrade_command(version="2.0.16")
        assert asked == ["https://mirror.example.com/simple/synapse-orch-ai/"]
        assert "still reaching your package index" in capsys.readouterr().out

    @pytest.mark.parametrize("var, value", [("UV_FIND_LINKS", "/tmp/wheels"),
                                            ("UV_INDEX", "https://extra.example.com/simple"),
                                            ("UV_EXTRA_INDEX_URL", "https://extra.example.com/simple")])
    def test_where_the_install_could_find_it_elsewhere_the_check_stands_aside(self, monkeypatch, tmp_path, var, value):
        seen = _world(monkeypatch, tmp_path)
        monkeypatch.setenv(var, value)
        _answer(monkeypatch, ["2.0.15"])
        cli._upgrade_command(version="2.0.16")
        assert seen == ["2.0.16"]

    def test_an_index_named_in_a_uv_toml_makes_the_check_stand_aside(self, monkeypatch, tmp_path):
        seen = _world(monkeypatch, tmp_path)
        conf = tmp_path / "config" / "uv"
        conf.mkdir(parents=True)
        (conf / "uv.toml").write_text('[[index]]\nurl = "https://mirror.example.com/simple"\ndefault = true\n')
        _answer(monkeypatch, ["2.0.15"])
        cli._upgrade_command(version="2.0.16")
        assert seen == ["2.0.16"]


# ── npm ───────────────────────────────────────────────────────────────────────

class TestNpmWaitsForItsRegistry:
    def _registry(self, monkeypatch, versions):
        try:
            index = _index()
        except ImportError:
            return
        monkeypatch.setattr(index, "_read_npm", lambda: None if versions is None else {"versions": {
            v: {} for v in versions}})

    def test_a_version_npm_lacks_is_said_plainly_never_the_command(self, monkeypatch, tmp_path, capsys):
        _world(monkeypatch, tmp_path, method="npm")
        _answer(monkeypatch, ["2.0.15", "2.0.16"])
        self._registry(monkeypatch, ["2.0.15"])
        with pytest.raises(SystemExit):
            cli._upgrade_command(version="2.0.16")
        out = capsys.readouterr().out
        assert "Synapse 2.0.16 is still reaching npm" in out and "npm install -g" not in out, out

    def test_npm_having_it_prints_the_command_as_before(self, monkeypatch, tmp_path, capsys):
        _world(monkeypatch, tmp_path, method="npm")
        _answer(monkeypatch, ["2.0.15", "2.0.16"])
        self._registry(monkeypatch, ["2.0.15", "2.0.16"])
        cli._upgrade_command(version="2.0.16")
        assert "npm install -g synapse-orch-ai@2.0.16" in capsys.readouterr().out


# ── neither installer resolves from a stale copy of its own ─────────────────

def test_pips_upgrade_skips_its_cache(monkeypatch):
    monkeypatch.setattr(cli, "_installed_extras", lambda: [])
    assert "--no-cache-dir" in cli._pip_upgrade_args("2.0.16")
