"""
Provider credentials never reach the process environment.

The engine used to hand a worker its settings by writing every one of them into
`os.environ` at startup — `inject_llm_env` produced `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `AWS_SECRET_ACCESS_KEY` and a `SYNAPSE_SETTING_*` variable
per key — and `load_settings()` read them back out. Bedrock had a second path
that set `AWS_BEARER_TOKEN_BEDROCK` and never cleared it.

Three things were wrong with that, in increasing order of seriousness:

1. A worker's settings were fixed for its lifetime, so one process could only
   ever serve one tenant.
2. Anything in the environment is readable in `/proc/<pid>/environ`.
3. Subprocesses inherit it — and this engine spawns stdio MCP servers and a
   transform sandbox, both of which would have been handed every provider key
   the worker held, including keys belonging to a different tenant.

This is the pilot gate's "`strings /proc/<worker_pid>/environ` contains no
provider key", asserted here rather than by hand.
"""
import os

import pytest

from core.config import default_settings, load_settings, set_settings_provider

#: Environment variables the deleted `_KEY_TO_ENV` map used to populate.
LEAKED_ENV_VARS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROK_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_COMPATIBLE_KEY",
    "LOCAL_COMPATIBLE_KEY",
    "HF_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "BEDROCK_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "S3_SECRET_ACCESS_KEY",
]

SENTINEL = "-".join(["sk", "test", "leakcanary", "0123456789"])


@pytest.fixture(autouse=True)
def _restore_provider():
    yield
    set_settings_provider(None)


def test_the_env_injection_module_is_gone():
    """`core.scale.llm_keys` was the whole mechanism. It should not import."""
    with pytest.raises(ImportError):
        import core.scale.llm_keys  # noqa: F401


def test_loading_settings_writes_nothing_to_the_environment():
    before = dict(os.environ)

    settings = default_settings()
    for key in ("openai_key", "anthropic_key", "aws_secret_access_key", "bedrock_api_key"):
        settings[key] = SENTINEL
    set_settings_provider(lambda: settings)

    load_settings()

    assert os.environ == before, "load_settings() mutated the process environment"
    assert not [v for v in os.environ.values() if SENTINEL in v]


def test_the_synapse_setting_overlay_is_gone(monkeypatch):
    """`SYNAPSE_SETTING_*` let anything in the environment override a setting.

    That is how the keys got in, and it also meant a variable set by an
    unrelated process on the same host could redirect a model call.
    """
    monkeypatch.setenv("SYNAPSE_SETTING_OPENAI_KEY", f'"{SENTINEL}"')
    monkeypatch.setenv("SYNAPSE_SETTING_MODEL", '"injected-model"')
    set_settings_provider(None)

    settings = load_settings()

    assert settings.get("openai_key") != SENTINEL
    assert settings.get("model") != "injected-model"


def test_building_a_bedrock_client_leaves_no_key_in_the_environment():
    """The Bedrock key was set process-wide and never cleared, so it outlived
    the job that supplied it and was inherited by the next tenant's
    subprocesses."""
    from core.llm_providers import _make_aws_client

    settings = {"bedrock_api_key": SENTINEL, "aws_region": "us-east-1"}
    _make_aws_client("bedrock-runtime", "us-east-1", settings)

    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    assert not [v for v in os.environ.values() if SENTINEL in v]


def test_a_bearer_client_needs_no_aws_credentials(monkeypatch):
    """The deployment this exists for has no AWS credentials at all.

    Without signature_version=UNSIGNED, botocore runs SigV4 and raises
    NoCredentialsError *before* the before-send hook that attaches the bearer
    token can fire.
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")

    from botocore import UNSIGNED
    from core.llm_providers import _make_aws_client

    client = _make_aws_client("bedrock-runtime", "us-east-1", {"bedrock_api_key": SENTINEL})
    assert client.meta.config.signature_version is UNSIGNED


def test_two_bedrock_clients_do_not_share_a_key():
    """The property that makes a shared fleet safe: the token is attached to
    the client, so interleaved calls cannot pick up each other's."""
    from core.llm_providers import _make_aws_client

    a = _make_aws_client("bedrock-runtime", "us-east-1", {"bedrock_api_key": "key-acme"})
    b = _make_aws_client("bedrock-runtime", "us-east-1", {"bedrock_api_key": "key-globex"})

    def _auth_for(client):
        captured = {}

        class _Req:
            headers: dict = {}

        request = _Req()
        request.headers = captured
        client.meta.events.emit("before-send.bedrock-runtime.Converse", request=request)
        return captured.get("Authorization")

    assert _auth_for(a) == "Bearer key-acme"
    assert _auth_for(b) == "Bearer key-globex"


@pytest.mark.parametrize("var", LEAKED_ENV_VARS)
def test_no_provider_key_variable_is_set_by_importing_the_engine(var):
    """Importing the engine must not populate any of these.

    Parametrised so a failure names the exact variable that came back.
    """
    import core.llm_providers  # noqa: F401
    import core.scale.worker  # noqa: F401
    import core.server  # noqa: F401

    assert var not in os.environ or os.environ[var] == "", (
        f"{var} was set by importing the engine"
    )
