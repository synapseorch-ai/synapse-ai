"""A transform's code runs with no network and none of the server's secrets (F968, REL2's sizing; Naveen 2026-10-24).

Before 2.0 every transform asked its runner for the internet (the sandbox's default bridge: the internet, the LAN,
the host's own ports, a cloud's metadata service), any client could switch transforms to the host runtime through
`POST /api/settings`, and the host runner handed the script the server's whole environment — its internal token,
its session-signing secret, its database URL. A transform is code from a person, a builder or an imported file.

Now: OSS's own runners run a transform with no network — tools are the way out; host is the server operator's
choice, made with `SYNAPSE_TRANSFORM_RUNTIME=host`, never a stored setting; and the host runner's child gets a short
allowlist of the environment, never a token, key, secret or URL. Host still cannot block the network (there is no
OS sandbox), which is why it is the operator's explicit choice. A runner a deployment registers (cloud's) decides its
own network and is asked as before.
"""
import json

from _fakes import seed as S

NO_NETWORK = "Transforms have no network since 2.0: fetch with a tool step and pass the result in."


def _server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class _Spy:
    """A runner that records what it was asked and answers like a transform that printed `result`."""

    def __init__(self, base=None, *, exit_code=0, stderr=""):
        self.asked = []
        self.base, self.exit_code, self.stderr = base, exit_code, stderr

    async def run(self, request):
        from core.execution import ExecResult
        self.asked.append(request)
        return ExecResult(exit_code=self.exit_code, stdout=json.dumps({"result": 1}) if not self.exit_code else "",
                          stderr=self.stderr)


def _as(kind, **kw):
    """A spy that is an instance of DockerRunner / HostRunner (the engine's own runners), or of neither."""
    from core.execution import DockerRunner, HostRunner
    base = {"docker": DockerRunner, "host": HostRunner, "registered": object}[kind]
    spy_cls = type(f"Spy{kind.title()}", (_Spy, base), {})
    obj = spy_cls.__new__(spy_cls)
    _Spy.__init__(obj, **kw)
    return obj


async def _transform(monkeypatch, *, runner_kind="docker", env_host=False, stored_host=False, spy=None, run_id):
    import core.config as config
    import core.execution as execution
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    spy = spy or _as(runner_kind)
    monkeypatch.setattr(execution, "get_runner", lambda: spy if runner_kind != "host" else _as("docker"))
    monkeypatch.setattr(execution, "HostRunner", lambda: spy if runner_kind == "host" else _as("host"))
    if env_host:
        monkeypatch.setenv("SYNAPSE_TRANSFORM_RUNTIME", "host")
    else:
        monkeypatch.delenv("SYNAPSE_TRANSFORM_RUNTIME", raising=False)
    monkeypatch.setattr(config, "load_settings",
                        lambda: {"transform_runtime": "host" if stored_host else "docker", "model": "mistral"})
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="t", steps=[
        {"id": "t", "name": "Shape", "type": "transform", "transform_code": "result = 1",
         "output_key": "out", "timeout_seconds": 30, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server()).run(
        "go", run_id=run_id)]
    return spy, events


class TestTheNetwork:
    async def test_the_sandbox_runs_a_transform_with_no_network(self, monkeypatch):
        from core.execution import Network
        spy, _ = await _transform(monkeypatch, run_id="run_f968_docker")
        assert [r.network for r in spy.asked] == [Network.NONE]

    async def test_a_runner_the_deployment_registered_is_asked_as_before(self, monkeypatch):
        """Cloud's runner applies its own rule (an org's allowed domains): F968 does not move it."""
        from core.execution import Network
        spy, _ = await _transform(monkeypatch, runner_kind="registered", run_id="run_f968_registered")
        assert [r.network for r in spy.asked] == [Network.INTERNET]

    async def test_host_cannot_block_it_and_is_the_operators_choice(self, monkeypatch):
        from core.execution import Network
        spy, _ = await _transform(monkeypatch, runner_kind="host", env_host=True, run_id="run_f968_host")
        assert [r.network for r in spy.asked] == [Network.INTERNET]

    async def test_a_failure_to_reach_the_network_says_why(self, monkeypatch):
        from core.orchestration.state import SharedState
        spy = _as("docker", exit_code=1, stderr=(
            'Traceback (most recent call last):\n  File "/sandbox/script.py", line 9, in <module>\n'
            "urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>\n"))
        await _transform(monkeypatch, spy=spy, run_id="run_f968_said")
        run = (await SharedState.restore("run_f968_said")).run
        assert NO_NETWORK in str(run.step_history[-1].get("error")), run.step_history[-1]

    async def test_any_other_failure_says_nothing_of_the_network(self, monkeypatch):
        from core.orchestration.state import SharedState
        spy = _as("docker", exit_code=1, stderr=(
            'Traceback (most recent call last):\n  File "/sandbox/script.py", line 9, in <module>\n'
            "ValueError: bad total\n"))
        await _transform(monkeypatch, spy=spy, run_id="run_f968_other")
        run = (await SharedState.restore("run_f968_other")).run
        assert NO_NETWORK not in str(run.step_history[-1].get("error"))


class TestTheHostRuntime:
    async def test_a_stored_setting_no_longer_picks_it(self, monkeypatch):
        spy, _ = await _transform(monkeypatch, stored_host=True, run_id="run_f968_stored")
        assert spy.asked, "the sandbox ran it: a stored 'host' is not the operator's choice"

    async def test_the_operators_env_var_does(self, monkeypatch):
        spy, _ = await _transform(monkeypatch, runner_kind="host", env_host=True, run_id="run_f968_env")
        assert spy.asked


class TestTheHostRunnersEnvironment:
    async def test_the_child_gets_none_of_the_servers_secrets(self, monkeypatch):
        """REL2's repro: a host transform printing its environment."""
        from core.execution import ExecRequest, HostRunner, Network

        secrets = {"SYNAPSE_INTERNAL_TOKEN": "tok", "SYNAPSE_JWT_SECRET": "jwt", "SYNAPSE_ACCESS_TOKEN": "acc",
                   "SYNAPSE_DB_URL": "postgresql://u:pw@db/x", "GITHUB_TOKEN": "gh", "S3_SECRET_ACCESS_KEY": "s3",
                   "AWS_SECRET_ACCESS_KEY": "aws", "REDIS_URL": "redis://r", "METRICS_TOKEN": "m",
                   "OPENAI_API_KEY": "sk-x", "SOME_PASSWORD": "p"}
        for k, v in secrets.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HF_HOME", "/models")
        code = "import os, json\nprint(json.dumps(dict(os.environ)))\n"
        result = await HostRunner().run(ExecRequest(code=code, network=Network.INTERNET, timeout_s=20))
        env = json.loads(result.stdout)
        assert not set(secrets) & set(env), sorted(set(secrets) & set(env))
        assert not [k for k in env if k.startswith("SYNAPSE_")]
        assert env.get("HF_HOME") == "/models", "what host exists for still flows"
        assert "PATH" in env


class TestAnUpgradedInstallIsTold:
    """REL2: an install whose settings chose "host" before 2.0 now runs its transforms in the sandbox, with no
    network; its GPU transforms would just start failing. Said once at start."""

    def test_a_stored_host_without_the_env_var_is_told(self, monkeypatch):
        from core.routes.settings import transform_runtime_notice
        monkeypatch.delenv("SYNAPSE_TRANSFORM_RUNTIME", raising=False)
        said = transform_runtime_notice({"transform_runtime": "host"})
        assert said and "since 2.0 only SYNAPSE_TRANSFORM_RUNTIME=host does" in said

    def test_the_operators_host_is_warned_of_what_it_is(self, monkeypatch):
        from core.routes.settings import transform_runtime_notice
        monkeypatch.setenv("SYNAPSE_TRANSFORM_RUNTIME", "host")
        said = transform_runtime_notice({"transform_runtime": "docker"})
        assert said and "including stored keys" in said

    def test_a_sandboxed_install_hears_nothing(self, monkeypatch):
        from core.routes.settings import transform_runtime_notice
        monkeypatch.delenv("SYNAPSE_TRANSFORM_RUNTIME", raising=False)
        assert transform_runtime_notice({"transform_runtime": "docker"}) is None
