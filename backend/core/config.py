import os
import json
import secrets as _secrets
from pathlib import Path
from urllib.parse import urlparse, urlunparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_data_dir_env = os.getenv("SYNAPSE_DATA_DIR", "")
if _data_dir_env:
    _p = Path(_data_dir_env)
    DATA_DIR = str(_p if _p.is_absolute() else _PROJECT_ROOT / _p)
else:
    DATA_DIR = str(Path(__file__).resolve().parent.parent / "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "credentials.json")
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")


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


def load_settings():
    default_settings = {
        "agent_name": "Synapse",
        "model": "ollama.mistral",
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
        "huggingface_token": "",
        "huggingface_models": "",
        "huggingface_max_new_tokens": 1024,
        "anthropic_cli_models": "",
        "gemini_cli_models": "",
        "codex_cli_models": "",
        "github_copilot_cli_models": "",
        "bedrock_api_key": "",
        "bedrock_inference_profile": "",
        "embedding_model": "",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_session_token": "",
        "aws_region": "us-east-1",
        "sql_connection_string": "",
        "n8n_url": "http://localhost:5678",
        "n8n_api_key": "",
        "n8n_table_id": "",
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
        "coding_agent_enabled": True,
        "report_agent_enabled": True,
        "messaging_enabled": True,
        "embed_code": False,
        "bash_allowed_dirs": [],
        "login_enabled": False,
        "login_username": "",
        "login_password_hash": "",
        # Scale / distributed execution
        "redis_url": "",
        "scale_postgres_url": "",
        "scale_mode_enabled": False,
        "scale_auto_sync": False,
        "worker_concurrency": 10,
        "otlp_endpoint": "",
        "metrics_token": "",
        "max_global_queue_depth": 1_000_000,
        "rate_limit_per_tenant_rps": 1000,
        "pgbouncer_mode": False,
        "num_queue_shards": 1,
    }
    
    if not os.path.exists(SETTINGS_FILE):
        file_settings = {}
    else:
        try:
            with open(SETTINGS_FILE, 'r') as f:
                file_settings = json.load(f)
        except Exception as e:
            print(f"DEBUG: Error loading settings: {e}")
            file_settings = {}

    settings = {**default_settings, **file_settings}

    # In scale worker mode, inject_llm_env() populates SYNAPSE_SETTING_* env vars
    # from Postgres. Overlay them here so all callers of load_settings() see the
    # Postgres-sourced values without needing access to the local settings.json.
    _prefix = "SYNAPSE_SETTING_"
    for _env_key, _env_val in os.environ.items():
        if _env_key.startswith(_prefix):
            _setting_key = _env_key[len(_prefix):].lower()
            try:
                settings[_setting_key] = json.loads(_env_val)
            except Exception:
                settings[_setting_key] = _env_val

    return settings


def get_or_create_jwt_secret() -> str:
    """Return SYNAPSE_JWT_SECRET from the environment or .env file.

    Persistence is handled by the CLI (synapse/cli.py) before the server starts.
    If the secret is missing here (e.g. server run directly without the CLI),
    an ephemeral in-memory value is used for this session only.
    """
    env_file = _PROJECT_ROOT / ".env"
    var = "SYNAPSE_JWT_SECRET"

    existing = os.environ.get(var, "")
    if existing:
        return existing

    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
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
