"""
Settings, personal details, and config endpoints.
"""
import os
import json
import shutil
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import load_settings, sanitize_db_url
from core.models import Settings, PersonalDetails
from core.personal_details import load_personal_details, save_personal_details
from core.llm_providers import _make_aws_client, OLLAMA_MODEL

router = APIRouter()


class EmbedSetupRequest(BaseModel):
    host: str = "localhost"
    port: int = 5432
    username: str = "postgres"
    password: str = ""
    db_name: str = "synapse"


class LoginConfigRequest(BaseModel):
    login_enabled: bool
    login_username: str = ""
    login_password: str = ""

# Path to the examples directory (sibling of this file's package root)
_EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "examples")


async def save_settings(settings: dict):
    """Persist settings for the current tenant and refresh the live snapshot.

    Refreshes rather than merely invalidating, because the rest of this request
    must see the write: update_settings restarts the Filesystem MCP server from
    the new bash_allowed_dirs immediately after saving.
    """
    from core import settings_runtime
    from core.store.settings import save_many

    await save_many(settings)
    await settings_runtime.refresh()


def _init_memory_store(settings: dict):
    """Initialize the long-term memory store.

    Chat memory always uses Ollama with nomic-embed-text (384d) — a small, fast,
    local model that stays consistent regardless of the LLM provider mode.
    Code embeddings use provider models separately (see services/code_indexer.py).
    """
    try:
        from core.memory import MemoryStore as _MemoryStore
    except ImportError:
        return None

    return _MemoryStore(model="nomic-embed-text", embed_fn=None)


# --- Status & Settings ---

@router.get("/api/status")
async def get_status():
    from core.routes.agents import load_user_agents, active_agent_id
    from core.images import model_can_see_images
    from core.llm_providers import detect_provider_from_model

    user_agents = await load_user_agents()
    current_settings = load_settings()
    default_model = current_settings.get("model", "mistral")

    agents_status = {}
    for a in user_agents:
        # Whether attaching an image to this agent does anything. DeepSeek and
        # HuggingFace log a warning and discard; the CLI providers discard in
        # silence. Answered here rather than in the browser so the list of
        # providers that cannot see does not need a second copy in TypeScript.
        agents_status[a["id"]] = {
            "name": a["name"],
            "status": "online",
            "can_see_images": model_can_see_images(a.get("model") or default_model),
        }

    # Resolve active agent's model
    active_agent = next((a for a in user_agents if a["id"] == active_agent_id), None)
    resolved_model = default_model
    if active_agent and active_agent.get("model"):
        resolved_model = active_agent["model"]

    provider = detect_provider_from_model(resolved_model)

    return {
        "agents": agents_status,
        "active_agent_id": active_agent_id,
        "overall": "operational",
        "model": resolved_model,
        "mode": current_settings.get("mode", "local"),
        "provider": provider,
    }


@router.get("/api/settings")
async def get_settings():
    # A copy: the hash is left out of the answer, never out of the settings (S10).
    settings = dict(load_settings())
    settings.pop("login_password_hash", None)
    return settings


@router.post("/api/settings/login")
async def update_login_settings(body: LoginConfigRequest):
    from core.user_auth import hash_password
    existing = load_settings()
    existing["login_enabled"] = body.login_enabled
    if body.login_enabled:
        if not body.login_username.strip():
            raise HTTPException(status_code=400, detail="Username is required when login is enabled")
        existing["login_username"] = body.login_username.strip()
        if body.login_password:
            existing["login_password_hash"] = hash_password(body.login_password)
        elif not existing.get("login_password_hash"):
            raise HTTPException(status_code=400, detail="Password is required for first-time login setup")
    await save_settings(existing)
    return {"status": "ok", "login_enabled": body.login_enabled}


@router.post("/api/settings")
async def update_settings(settings: Settings):
    _safe = {k: (v[:4] + '…' + v[-4:] if isinstance(v, str) and len(v) > 12 and any(kw in k for kw in ('key', 'token', 'secret', 'password')) else v) for k, v in settings.dict(exclude_unset=True).items()}
    print(f"DEBUG: update_settings called with keys: {list(_safe.keys())}")
    # Get the latest payload and strip unset values to avoid overwriting existing properties with defaults
    try:
        data = settings.dict(exclude_unset=True)
    except Exception:
        data = settings.dict()

    existing = load_settings()
    # Never overwrite the bcrypt hash via the general settings endpoint
    data.pop("login_password_hash", None)
    if "tool_policy_defaults" in data:
        from core.tool_policy import clean_policy

        data["tool_policy_defaults"] = clean_policy(data["tool_policy_defaults"])
    prev_bash_dirs = list(existing.get("bash_allowed_dirs", []))
    existing.update(data)
    data = existing

    await save_settings(data)

    # Reinitialize memory so embeddings provider matches the new mode.
    import core.server as _server

    # If the user's allowed directories changed, restart the Filesystem MCP
    # server so its allowed roots match what the bash tool sees.
    if list(data.get("bash_allowed_dirs", [])) != prev_bash_dirs:
        try:
            await _server.restart_filesystem_mcp()
        except Exception as e:
            print(f"Warning: Failed to restart filesystem MCP after settings update: {e}")
    try:
        from core.memory import MemoryStore as _MemoryStore
    except ImportError:
        _MemoryStore = None
    
    if _MemoryStore:
        try:
            _server.memory_store = _init_memory_store(data)
        except Exception as e:
            print(f"Warning: failed to reinitialize MemoryStore after settings update: {e}")
    return data


# --- Code Embedding Setup ---

@router.get("/api/settings/check-embed")
async def check_embed_setup():
    """Check if PostgreSQL + pgvector are correctly set up for code embedding."""
    settings = load_settings()
    db_url = sanitize_db_url(settings.get("sql_connection_string", ""))

    # Build a masked hint (no password) for the frontend to display
    db_url_hint = ""
    if db_url:
        try:
            from urllib.parse import urlparse
            p = urlparse(db_url)
            db_url_hint = f"{p.scheme}://{p.hostname}:{p.port or 5432}{p.path}"
        except Exception:
            db_url_hint = db_url[:40] + "…" if len(db_url) > 40 else db_url

    result = {
        "psql_available": shutil.which("psql") is not None,
        "db_url_configured": bool(db_url),
        "db_url_hint": db_url_hint,
        "db_connection_ok": False,
        "pgvector_available": False,
        "all_ok": False,
    }

    if db_url:
        try:
            import psycopg  # type: ignore
            with psycopg.connect(db_url, connect_timeout=5) as conn:
                result["db_connection_ok"] = True
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                    if cur.fetchone() is not None:
                        result["pgvector_available"] = True
                    else:
                        # Extension missing — create it now
                        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        conn.commit()
                        result["pgvector_available"] = True
        except Exception as e:
            result["db_error"] = str(e)

    result["all_ok"] = all([
        result["psql_available"],
        result["db_url_configured"],
        result["db_connection_ok"],
        result["pgvector_available"],
    ])
    return result


@router.post("/api/settings/setup-embed")
async def setup_embed(body: EmbedSetupRequest):
    """Create a PostgreSQL database, enable pgvector, and save the connection string."""
    try:
        import psycopg  # type: ignore

        password_part = f":{body.password}" if body.password else ""
        db_url = f"postgresql://{body.username}{password_part}@{body.host}:{body.port}/{body.db_name}"
        admin_url = f"postgresql://{body.username}{password_part}@{body.host}:{body.port}/postgres"

        # Create the database if it doesn't exist
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (body.db_name,))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{body.db_name}"')

        # Enable pgvector in the target database
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()

        # Persist the connection string
        existing = load_settings()
        existing["sql_connection_string"] = db_url
        await save_settings(existing)

        return {"status": "ok", "connection_string": db_url}
    except ImportError:
        raise HTTPException(status_code=500, detail="psycopg not installed. Run: pip install psycopg[binary]")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Personal Details ---

@router.get("/api/personal-details")
async def get_personal_details_api():
    return await load_personal_details()


@router.post("/api/personal-details")
async def update_personal_details_api(details: PersonalDetails):
    data = details.dict()
    return await save_personal_details(data)


# --- Google Credentials & Config ---

# ── Global variables ──────────────────────────────────────────────────────────
#
# `{{ global.key }}` in tool URLs, headers and bodies, in agent system prompts
# and in orchestration prompt templates — core/variables.py for the store,
# core/templating.py for the syntax.


@router.get("/api/settings/variables")
async def get_global_variables():
    from core import variables
    return {"values": await variables.load()}


@router.post("/api/settings/variables")
async def set_global_variables(payload: dict):
    """Replace the whole document. Body: {"values": {key: value}}."""
    from core import variables
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="Body must include a 'values' object.")
    bad = [k for k in values if not isinstance(k, str) or not variables.valid_key(k)]
    if bad:
        raise HTTPException(status_code=400, detail=f"Variable names must be letters, numbers, hyphens or underscores, and not start with a digit: {bad}")
    await variables.save({k: v for k, v in values.items()})
    return {"values": await variables.load()}


@router.post("/api/setup/google-credentials")
async def upload_google_creds(request: Request):
    try:
        data = await request.json()
        print(f"DEBUG: Received credentials upload (Type: {type(data)})")

        if isinstance(data, str):
            parsed = json.loads(data)
        else:
            parsed = data

        from services.google import save_client_config
        await save_client_config(parsed)

        return {"status": "success", "message": "Credentials saved successfully."}
    except Exception as e:
        print(f"Error saving credentials: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")


@router.post("/api/setup/google-token")
async def upload_google_token(request: Request):
    try:
        data = await request.json()
        print(f"DEBUG: Received token upload (Type: {type(data)})")

        if isinstance(data, str):
            parsed = json.loads(data)
        else:
            parsed = data

        from services.google import save_token
        await save_token(parsed)

        return {"status": "success", "message": "Token saved successfully."}
    except Exception as e:
        print(f"Error saving token: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")


@router.get("/api/config")
async def get_config():
    from services import google as google_svc

    client_config = await google_svc.load_client_config()
    if not client_config:
        return {"has_credentials": False, "is_connected": False}

    try:
        app_info = client_config.get("web") or client_config.get("installed", {})

        client_id_full = app_info.get("client_id", "")
        # Mask: show only last 4 chars, e.g. ****h453
        masked_client_id = ("****" + client_id_full[-8:]) if len(client_id_full) > 8 else "****"

        token_data = await google_svc.load_token() or {}
        user_email = google_svc._email_from_token_data(token_data)

        # Whether the stored token is currently usable. Deliberately does NOT
        # refresh: this is a GET the integrations tab polls, and refreshing
        # here meant a status read performed a network token exchange and
        # rewrote three files every time the page was opened. The refresh
        # happens where it belongs — when workspace-mcp is about to be spawned.
        is_connected = False
        try:
            is_connected = await google_svc.get_google_credentials(refresh=False) is not None
        except Exception as e:
            print(f"Warning: Google credential check failed: {e}")

        return {
            "has_credentials": True,
            "client_id": masked_client_id,
            "project_id": app_info.get("project_id", ""),
            "is_connected": is_connected,
            "user_email": user_email,
        }
    except Exception as e:
        return {"has_credentials": True, "error": str(e), "is_connected": False}


@router.get("/api/file")
async def get_file(path: str):
    """Serve a local file. Restricted to the tenant's vault and configured repos.

    This used to allow the whole home directory alongside the old data
    directory. Once the vault moved out of it, "home directory" became the only
    allowance that matched anything — turning a vault-file endpoint into a read
    primitive over every file the server user owns. The allowance is now the two
    directories whose contents this endpoint exists to serve.
    """
    from core.server import _get_repo_paths
    from core.vault import _vault_root

    resolved = os.path.realpath(path)
    allowed_bases = [str(_vault_root())] + await _get_repo_paths()
    if not any(resolved.startswith(os.path.realpath(base)) for base in allowed_bases):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed directories")
    if not os.path.exists(resolved) or not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved)


# --- Example Packs ---

@router.get("/api/examples")
async def get_examples():
    """Return the list of available example packs from backend/examples/index.json."""
    index_path = os.path.join(_EXAMPLES_DIR, "index.json")
    if not os.path.exists(index_path):
        return []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load examples index: {e}")


@router.get("/api/examples/{example_id}")
async def get_example_bundle(example_id: str):
    """Return the import bundle JSON for a specific example pack."""
    # Sanitize: only alphanumeric and underscores
    safe_id = "".join(c for c in example_id if c.isalnum() or c == "_")
    bundle_path = os.path.realpath(os.path.join(_EXAMPLES_DIR, f"{safe_id}.bundle.json"))
    # Ensure it stays within the examples directory
    if not bundle_path.startswith(os.path.realpath(_EXAMPLES_DIR)):
        raise HTTPException(status_code=403, detail="Invalid example ID")
    if not os.path.exists(bundle_path):
        raise HTTPException(status_code=404, detail=f"Example pack '{example_id}' not found")
    try:
        with open(bundle_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load example bundle: {e}")
