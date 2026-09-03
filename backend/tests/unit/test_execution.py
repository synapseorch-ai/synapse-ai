"""`core.execution` — the runner registry, the moved Docker logic, and the canary.

The canary is the reason this file exists. `allow_network` was accepted by
`tools/sandbox.py::_build_docker_cmd`, threaded through three signatures, and
never once reached the `docker run` argv — so every sandbox in the shipped
product ran with network access, including the calls that asked not to. No test
caught it because no test ever asserted the *effect*, only the plumbing.

`test_network_none_actually_blocks_a_url` asserts the effect. It needs a real
Docker daemon and the sandbox image, so it skips where those are absent — but it
is the one test in here that would have caught the original bug, and a run that
skips it should say so rather than report green.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from core.execution import (
    RUNNER_ENV,
    DockerRunner,
    ExecRequest,
    ExecResult,
    ExecUnsupported,
    HostRunner,
    Network,
    get_runner,
    register_runner,
    wrap_args,
    wrap_transform,
)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """No test inherits another's runner, or the ambient environment's."""
    monkeypatch.delenv(RUNNER_ENV, raising=False)
    register_runner(None)
    yield
    register_runner(None)


def _has_docker_image() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        out = subprocess.run(
            ["docker", "images", "-q", "sandbox-python:latest"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return False
    return bool(out.stdout.strip())


needs_sandbox = pytest.mark.skipif(
    not _has_docker_image(),
    reason="needs a Docker daemon and the sandbox-python:latest image",
)


class TestTheDockerCommandIsTheOneThatWasThere:
    """The logic moved out of five call sites; it did not get redesigned."""

    def test_the_limits_are_unchanged(self):
        cmd = DockerRunner().build_command(
            ExecRequest(code="print(1)", network=Network.INTERNET), "/tmp/s.py"
        )
        joined = " ".join(cmd)
        assert joined.startswith("docker run --rm ")
        for flag in ("--memory 512m", "--cpus 1.0", "--pids-limit 64", "--read-only",
                     "--tmpfs /tmp:rw,size=256m", "--tmpfs /root:rw,size=256m"):
            assert flag in joined, flag
        assert joined.endswith("sandbox-python:latest python /sandbox/script.py")
        assert "-v /tmp/s.py:/sandbox/script.py:ro" in joined

    def test_a_missing_vault_is_not_mounted(self, tmp_path):
        absent = tmp_path / "no-such-vault"
        cmd = DockerRunner().build_command(
            ExecRequest(code="", network=Network.INTERNET, vault_path=absent), "/tmp/s.py"
        )
        assert "/data" not in " ".join(cmd)

    def test_a_present_vault_is_mounted_read_only(self, tmp_path):
        cmd = DockerRunner().build_command(
            ExecRequest(code="", network=Network.INTERNET, vault_path=tmp_path), "/tmp/s.py"
        )
        assert f"-v {tmp_path}:/data:ro" in " ".join(cmd)

    def test_packages_still_go_through_pip_and_the_name_filter_still_bites(self):
        cmd = DockerRunner().build_command(
            ExecRequest(
                code="", network=Network.INTERNET,
                packages=("requests", "pandas==2.0", "evil; rm -rf /"),
            ),
            "/tmp/s.py",
        )
        inner = cmd[-1]
        assert "pip install" in inner and "requests pandas==2.0" in inner
        assert "rm -rf" not in inner
        assert inner.endswith("python /sandbox/script.py")


class TestNetworkIsAFieldARunnerAnswersFor:
    """The bug D8 named, and the two ways it is now prevented."""

    def test_none_puts_network_none_in_the_argv(self):
        cmd = DockerRunner().build_command(
            ExecRequest(code="", network=Network.NONE), "/tmp/s.py"
        )
        assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"

    def test_internet_leaves_the_default_bridge_alone(self):
        cmd = DockerRunner().build_command(
            ExecRequest(code="", network=Network.INTERNET), "/tmp/s.py"
        )
        assert "--network" not in cmd

    def test_network_has_no_default_so_it_cannot_be_forgotten(self):
        with pytest.raises(TypeError):
            ExecRequest(code="print(1)")  # type: ignore[call-arg]

    async def test_packages_without_a_network_is_refused_not_silently_skipped(self):
        with pytest.raises(ExecUnsupported):
            await DockerRunner().run(
                ExecRequest(code="", network=Network.NONE, packages=("requests",))
            )

    @needs_sandbox
    async def test_network_none_actually_blocks_a_url(self):
        """The canary. Same code, two networks, two different answers."""
        code = (
            "import socket, json\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=4).close()\n"
            "    print(json.dumps({'reached': True}))\n"
            "except Exception:\n"
            "    print(json.dumps({'reached': False}))\n"
        )
        blocked = await DockerRunner().run(
            ExecRequest(code=code, network=Network.NONE, timeout_s=20)
        )
        assert '"reached": false' in blocked.stdout.lower(), blocked.stdout

        allowed = await DockerRunner().run(
            ExecRequest(code=code, network=Network.INTERNET, timeout_s=20)
        )
        assert '"reached": true' in allowed.stdout.lower(), allowed.stdout


class TestTheRegistryNeverGuesses:
    def test_unset_is_the_docker_runner(self):
        assert isinstance(get_runner(), DockerRunner)

    def test_a_registered_runner_wins(self):
        sentinel = HostRunner()
        register_runner(sentinel)
        assert get_runner() is sentinel

    def test_the_env_names_an_import_path(self, monkeypatch):
        monkeypatch.setenv(RUNNER_ENV, f"{__name__}:_fake_factory")
        assert isinstance(get_runner(), _FakeRunner)

    def test_a_spec_that_does_not_resolve_raises_rather_than_falling_back(self, monkeypatch):
        monkeypatch.setenv(RUNNER_ENV, "no.such.module:factory")
        with pytest.raises(RuntimeError) as exc:
            get_runner()
        # The message has to say why there is no fallback, because the person
        # reading it is looking at a deployment that just refused to run.
        assert "fallback" in str(exc.value)

    def test_a_spec_without_a_colon_is_refused(self, monkeypatch):
        monkeypatch.setenv(RUNNER_ENV, "core.execution.docker_runner.DockerRunner")
        with pytest.raises(RuntimeError):
            get_runner()


class TestTheScriptWrappersAreUnchanged:
    def test_args_arrive_as_a_dict_named_args(self):
        script = wrap_args("print(_args['x'])", {"x": 1})
        assert script.startswith("import json\n_args = json.loads(")
        assert script.endswith("print(_args['x'])")

    def test_a_triple_quote_in_a_value_does_not_break_out(self):
        script = wrap_args("pass", {"x": 'a"""b'})
        ns: dict = {}
        exec(compile(script, "<wrapped>", "exec"), ns)  # noqa: S102 — that is the assertion
        assert ns["_args"] == {"x": 'a"""b'}

    def test_a_backslash_in_a_value_survives(self):
        script = wrap_args("pass", {"p": "C:\\temp\\x"})
        ns: dict = {}
        exec(compile(script, "<wrapped>", "exec"), ns)  # noqa: S102
        assert ns["_args"] == {"p": "C:\\temp\\x"}

    def test_transform_prints_its_result_as_json(self, capsys):
        script = wrap_transform("result = state['n'] * 2", {"n": 21})
        exec(compile(script, "<wrapped>", "exec"), {})  # noqa: S102
        assert '"result": 42' in capsys.readouterr().out

    def test_transform_prints_null_when_nothing_was_assigned(self, capsys):
        exec(compile(wrap_transform("pass", {}), "<wrapped>", "exec"), {})  # noqa: S102
        assert '"result": null' in capsys.readouterr().out


class TestTheHostRunnerIsRealAndSeparate:
    async def test_it_runs_and_returns_stdout(self):
        result = await HostRunner().run(
            ExecRequest(code="print('hello from host')", network=Network.INTERNET, timeout_s=30)
        )
        assert result.ok and "hello from host" in result.stdout

    async def test_a_failure_is_reported_not_raised(self):
        result = await HostRunner().run(
            ExecRequest(code="raise SystemExit(3)", network=Network.INTERNET, timeout_s=30)
        )
        assert result.exit_code == 3 and not result.ok

    async def test_it_is_not_what_get_runner_returns_by_default(self):
        """A deployment has to opt in to the unsandboxed runtime, never inherit it."""
        assert not isinstance(get_runner(), HostRunner)


class TestExecResult:
    def test_ok_is_false_on_a_timeout_even_with_exit_zero(self):
        assert not ExecResult(exit_code=0, stdout="", stderr="", timed_out=True).ok


# --- module-level, so `RUNNER_ENV` can name it ------------------------------

class _FakeRunner:
    async def run(self, request: ExecRequest) -> ExecResult:
        return ExecResult(exit_code=0, stdout="fake", stderr="")


def _fake_factory() -> _FakeRunner:
    return _FakeRunner()
