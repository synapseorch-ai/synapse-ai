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
        "auto_compact_enabled": False,
        "auto_compact_threshold": 100000,
        "allow_db_write": False,
        "coding_agent_enabled": True,
        "report_agent_enabled": True,
        "messaging_enabled": True,
        "embed_code": False,
        "bash_allowed_dirs": [],
        "login_enabled": False,
        "login_username": "",
        "login_password_hash": "",
    }
    
    if not os.path.exists(SETTINGS_FILE):
        return default_settings
    
    try:
        with open(SETTINGS_FILE, 'r') as f:
            data = json.load(f)
            # Merge defaults
            return {**default_settings, **data}
    except Exception as e:
        print(f"DEBUG: Error loading settings: {e}")
        return default_settings


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
