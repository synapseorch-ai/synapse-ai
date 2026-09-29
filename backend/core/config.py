"""
Engine configuration: timeouts, the shipped settings defaults, and the settings
provider hook.

There is no `DATA_DIR` here, and there is no directory created at import. The
engine's documents live in `core/store/`, its tenant content in
`core/storage/`, and the handful of things that genuinely need a directory on
disk in `core/runtime_dirs.py`. One constant that meant all three is what made
the engine a thing you installed on a laptop rather than something a request
can be served by.
"""
import os
import secrets as _secrets
from urllib.parse import urlparse, urlunparse


# ── Configurable timeouts ────────────────────────────────────────────────────
# Each is read from the environment, falling back to the value hardcoded before
# these were made configurable. Unset env → identical behavior to before.

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return int(default)


# Timeouts (seconds, unless the name says otherwise).
MCP_SESSION_READ_TIMEOUT = _env_float("SYNAPSE_MCP_SESSION_READ_TIMEOUT", 60.0)
MCP_TOOL_CALL_TIMEOUT    = _env_float("SYNAPSE_MCP_TOOL_CALL_TIMEOUT", 60.0)
MCP_LIST_TOOLS_TIMEOUT   = _env_float("SYNAPSE_MCP_LIST_TOOLS_TIMEOUT", 15.0)
LLM_REQUEST_TIMEOUT      = _env_float("SYNAPSE_LLM_TIMEOUT", 180.0)
HTTP_TOOL_TIMEOUT        = _env_float("SYNAPSE_HTTP_TOOL_TIMEOUT", 30.0)
ORCH_STEP_TIMEOUT        = _env_float("SYNAPSE_ORCH_STEP_TIMEOUT", 300.0)
ORCH_GLOBAL_TIMEOUT_MIN  = _env_int("SYNAPSE_ORCH_GLOBAL_TIMEOUT_MINUTES", 30)
ORCH_HUMAN_TIMEOUT       = _env_float("SYNAPSE_ORCH_HUMAN_TIMEOUT", 3600.0)

# `run_bash`'s own timeout: its default and the most it accepts (tools/bash.py
# clamps a call's `timeout` to these), and so how long a caller waits for it.
BASH_TIMEOUT_DEFAULT = 30
BASH_TIMEOUT_MAX = 120


def mcp_call_timeout(tool_name: str, arguments: dict | None) -> float:
    """How long a caller waits for one MCP tool call, in seconds.

    `MCP_TOOL_CALL_TIMEOUT`, except for `run_bash`, which takes a timeout of its
    own: it is waited for that long plus 5 s for the reply (F300). It offers up
    to 120 s, and a caller that stopped at 60 s told the model the call timed out
    while the command ran on with nobody reading it. The command is killed at its
    own timeout, so a caller that waits this long hears how it ended.
    """
    if tool_name == "run_bash" and isinstance(arguments, dict):
        try:
            own = float(arguments.get("timeout") or BASH_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            own = float(BASH_TIMEOUT_DEFAULT)
        own = min(max(own, 1.0), float(BASH_TIMEOUT_MAX))
        return max(MCP_TOOL_CALL_TIMEOUT, own + 5.0)
    return MCP_TOOL_CALL_TIMEOUT
# A Parallel step's branches run at the same time, at most
# PARALLEL_MAX_BRANCHES at once (F255). A branch that can stop to ask a person,
# holds a Loop or a nested Parallel, drives the browser, or reads what another
# branch writes runs afterwards, in order (`ParallelStepExecutor`). Set
# SYNAPSE_PARALLEL_BRANCHES to 0 to run every branch one after another.
PARALLEL_BRANCHES_CONCURRENT = os.getenv("SYNAPSE_PARALLEL_BRANCHES", "1").strip().lower() not in ("0", "false", "no", "off")
PARALLEL_MAX_BRANCHES = max(1, _env_int("SYNAPSE_PARALLEL_MAX_BRANCHES", 4))


# ── Settings provider ────────────────────────────────────────────────────────
# `load_settings()` is called from ~16 places on the orchestration execution
# path, in modules that have no business knowing where settings come from. A
# provider hook lets an embedder answer all of them at once — per tenant, from
# a database, decrypting secrets on the way — without any of those call sites
# changing.
#
# Process-global by design. The *tenant* varies per execution and is read from
# a ContextVar (see core/tenancy.py); the *source* does not vary at all.

_settings_provider = None


def set_settings_provider(fn) -> None:
    """Install a callable returning the settings dict for the current tenant.

    `fn` takes no arguments and reads `core.tenancy.get_tenant()` if it needs
    to know whose settings to return. Passing None restores the built-in
    behaviour.
    """
    global _settings_provider
    _settings_provider = fn


def get_settings_provider():
    return _settings_provider


def default_settings() -> dict:
    """The shipped defaults, before any file, environment or provider overlay.

    Exposed so a provider can overlay a tenant's stored values onto the same
    baseline the engine expects, rather than reconstructing ~85 keys and
    silently omitting whichever ones it forgot.
    """
    return dict(_DEFAULTS)


def load_settings():
    """The current tenant's settings, or the shipped defaults.

    A provider is installed at startup (`core/settings_runtime.py`) and serves
    the store. Without one — during import, in a test that has not booted the
    engine, or in an embedder that has not registered anything yet — this is
    the shipped baseline. It is deliberately not a file read: settings were a
    JSON document under a data directory, and the last thing an engine serving
    many tenants should do is answer from one.
    """
    if _settings_provider is not None:
        return _settings_provider()
    return dict(_DEFAULTS)


#: Settings that are secrets (F201). `GET /api/settings` and the POST's answer
#: serve each as "" beside `<name>_set` and `<name>_hint`; a POST that sends one
#: blank keeps what is stored (the page posts its whole form back), and
#: `clear_secrets` removes one. A field named like a secret that is missing here
#: fails a test, so a new key is masked by default rather than by memory.
SECRET_SETTINGS = (
    "openai_key", "anthropic_key", "gemini_key", "grok_key", "deepseek_key",
    "openai_compatible_key", "local_compatible_key", "model_gateway_key",
    "huggingface_token", "bedrock_api_key", "aws_secret_access_key", "aws_session_token",
    "n8n_api_key", "metrics_token", "s3_secret_access_key",
)


#: Shipped defaults. Module-level so a settings provider can overlay a tenant's
#: stored values onto the same baseline rather than reconstructing every key.
_DEFAULTS = {
        "agent_name": "Synapse",
        "model": "ollama.mistral",
        # Which model the AI builder itself runs on. Empty means "whatever
        # `model` is" — the builder is a long multi-step run, so it is worth
        # being able to point it at a stronger model than the day-to-day one
        # without changing every agent.
        "builder_model": "",
        "judge_model": "",
        "mode": "local",
        "openai_key": "",
        "anthropic_key": "",
        "gemini_key": "",
        "grok_key": "",
        "deepseek_key": "",
        "openai_compatible_key": "",
        "openai_compatible_base_url": "",
        "openai_compatible_models": "",
        "local_compatible_base_url": "",
        "local_compatible_key": "",
        "local_compatible_models": "",
        "openai_compatible_embed_models": "",
        "local_compatible_embed_models": "",
        "model_gateway_base_url": "",
        "model_gateway_key": "",
        "model_gateway_models": "",
        "model_gateway_embed_models": "",
        "huggingface_token": "",
        "huggingface_models": "",
        "huggingface_max_new_tokens": 1024,
        "anthropic_cli_models": "",
        "gemini_cli_models": "",
        "codex_cli_models": "",
        "github_copilot_cli_models": "",
        "bedrock_api_key": "",
        "embedding_model": "",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_session_token": "",
        "aws_region": "us-east-1",
        "sql_connection_string": "",
        "n8n_url": "http://localhost:5678",
        "n8n_api_key": "",
        # Empty uses the OLLAMA_BASE_URL env var.
        "ollama_base_url": "",
        "global_config": {},
        "vault_enabled": True,
        "vault_threshold": 100000,
        "auto_compact_enabled": True,
        "auto_compact_threshold": 80000,
        # Prompt caching: decorate provider payloads with cache_control markers
        # so subsequent ReAct turns reuse the cached system + tools prefix.
        # ~50–80% cost reduction on multi-turn agents at the cost of a 25% write
        # surcharge on the first turn. Disable only if a provider misbehaves.
        "prompt_cache_enabled": True,
        # Transform step Python execution runtime.
        # "docker" (default): runs in the sandbox-python container — 512 MB / 1 CPU /
        # 60s, isolated from the host.
        # "host": runs as a subprocess on the host with full RAM, GPU, filesystem,
        # and network access. Required for HuggingFace / RecursiveMAS workflows that
        # need torch + GPU but removes the sandbox security boundary. Self-hosted
        # single-user deployments only.
        "transform_runtime": "docker",
        "allow_db_write": False,
        # Tool policy's deployment-wide floor — see core/tool_policy.py. Empty
        # is `allow` everywhere, deliberately: a closed default would change the
        # behaviour of every agent already running on upgrade.
        "tool_policy_defaults": {},
        # An install choice, read at startup — see core/models.py.
        "coding_agent_enabled": True,
        "messaging_enabled": True,
        "messaging_webhook_base_url": "",
        "embed_code": False,
        "bash_allowed_dirs": [],
        # ── MCP stdio server registration (security) ──
        # stdio MCP servers launch local OS processes (npx/uvx/python/…). This is
        # a powerful, admin-only capability. Allowed by default for local
        # single-user use, but force-disabled in scale mode (multi-tenant /
        # network-exposed) — see stdio_mcp_allowed(). See GHSA-3j67-x3j8-r32x.
        "allow_stdio_mcp": True,
        # Optional hardening: when non-empty, only these command basenames may be
        # registered as stdio servers. Empty = allow any command in PATH.
        # Recommended for exposed UIs: ["npx","uvx","uv","node","python","python3","docker"]
        "mcp_command_allowlist": [],
        "login_enabled": False,
        "login_username": "",
        "login_password_hash": "",
        # Scale / distributed execution
        "redis_url": "",
        "scale_postgres_url": "",
        "scale_mode_enabled": False,
            "worker_concurrency": 10,
        "otlp_endpoint": "",
        "metrics_token": "",
        "max_global_queue_depth": 1_000_000,
        "rate_limit_per_tenant_rps": 1000,
        "pgbouncer_mode": False,
}


# There is no settings file, and no SYNAPSE_SETTING_* overlay. Workers used to
# receive their settings by having every one of them — provider API keys
# included — written into the process environment at startup and read back out
# here. That put secrets in /proc/<pid>/environ and in every subprocess the
# worker spawns, and it fixed a worker's settings for its lifetime, which one
# shared fleet cannot do. Settings come from a provider; see
# set_settings_provider above.


def get_or_create_jwt_secret() -> str:
    """Return SYNAPSE_JWT_SECRET from the environment or .env file.

    Persistence is handled by the CLI (synapse/cli.py) before the server starts.
    If the secret is missing here (e.g. server run directly without the CLI),
    an ephemeral in-memory value is used for this session only.
    """
    from core.paths import env_file as _env_file

    env_file = _env_file()
    var = "SYNAPSE_JWT_SECRET"

    existing = os.environ.get(var, "")
    if existing:
        return existing

    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{var}=") and len(line) > len(f"{var}="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        os.environ[var] = val
                        return val
        except Exception:
            pass

    secret = _secrets.token_hex(32)
    os.environ[var] = secret
    print(
        f"Warning: {var} was not found; generated an ephemeral in-memory secret. "
        f"Set {var} in the environment (or run 'synapse start') to persist across restarts."
    )
    return secret


def sanitize_db_url(raw: str) -> str:
    """Normalize a PostgreSQL URL for use with psycopg (not SQLAlchemy).

    Fixes:
    1. Strips SQLAlchemy dialect suffix (e.g. postgresql+psycopg → postgresql)
    2. Rewrites empty password (user:@host → user@host) which psycopg/libpq cannot parse.
    """
    if not raw:
        return ""
    p = urlparse(raw)
    netloc = p.netloc
    if netloc:
        netloc = netloc.replace(":@", "@")
    scheme = p.scheme.split("+")[0]
    return urlunparse(p._replace(scheme=scheme, netloc=netloc))
