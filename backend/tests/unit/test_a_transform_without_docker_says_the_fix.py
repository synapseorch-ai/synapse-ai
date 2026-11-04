"""A Transform with no Docker to run in says the fix in plain words, never Docker's own (F1353, the engine half;
REL3's 1.9.3 → 2.0 upgrade, 11-04).

On REL3's upgraded install Docker wasn't running, and a Transform failed with the daemon's text: "failed to connect
to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running …".
It named a socket and no fix. Code steps run in the Docker sandbox unless the operator chose the host
(`SYNAPSE_TRANSFORM_RUNTIME=host`), so a Transform that finds no Docker — its daemon down, in any release's words,
or no `docker` at all — now says what to do, and nothing of the socket. A Transform's own failure still says its
line, as before (F407). REL3 adds the matching note at upgrade.
"""
from __future__ import annotations

import pytest

from core.execution import DockerRunner, ExecResult

WORDS = ("Code steps run in Docker, and Docker isn't running here. Start Docker, or set "
         "SYNAPSE_TRANSFORM_RUNTIME=host to run them on this computer.")
NEW = ("failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the "
       "daemon is running: dial unix /var/run/docker.sock: connect: no such file or directory")
OLD = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
WINDOWS = ("error during connect: Post \"http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.24/containers/create\": open "
           "//./pipe/docker_engine: The system cannot find the file specified.")


class _Docker(DockerRunner):
    """The Docker sandbox, as a machine without its daemon answers it."""

    def __init__(self, *, stderr: str = "", exit_code: int = 125):
        self.stderr, self.exit_code = stderr, exit_code

    async def run(self, request):
        return ExecResult(exit_code=self.exit_code, stdout="", stderr=self.stderr)


@pytest.fixture
def docker(monkeypatch):
    import core.execution as execution

    monkeypatch.delenv("SYNAPSE_TRANSFORM_RUNTIME", raising=False)

    def use(runner: DockerRunner) -> None:
        monkeypatch.setattr(execution, "get_runner", lambda: runner)

    return use


async def _failure(code: str = "result = 1") -> str:
    from core.orchestration.steps import TransformStepExecutor

    with pytest.raises(Exception) as caught:
        await TransformStepExecutor()._run(code, {})
    return str(caught.value)


@pytest.mark.parametrize("stderr", [NEW, OLD, WINDOWS], ids=["docker-api", "docker-daemon", "windows-pipe"])
async def test_a_daemon_that_is_not_running_says_the_fix(client, docker, stderr):
    docker(_Docker(stderr=stderr))
    said = await _failure()
    assert WORDS in said, said
    assert "unix://" not in said and "docker.sock" not in said and "pipe" not in said, said


async def test_no_docker_at_all_says_the_same(client, docker, monkeypatch):
    """The sandbox's own runner, on a machine with no `docker` to call."""
    monkeypatch.setattr("core.execution.docker_runner.shutil.which", lambda name: None)
    docker(DockerRunner())
    assert WORDS in await _failure()


async def test_a_transforms_own_failure_still_says_its_line(client, docker):
    traceback = ('Traceback (most recent call last):\n  File "/tmp/synapse_exec_x/script.py", line 40, in <module>\n'
                 "ValueError: boom\n")
    docker(_Docker(stderr=traceback, exit_code=1))
    said = await _failure("raise ValueError('boom')")
    assert "boom" in said and WORDS not in said, said
