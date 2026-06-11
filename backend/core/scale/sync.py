"""
Sync local JSON-file data to Postgres.
Called from the "Sync Now" button in the Scale settings tab and optionally
on every CRUD operation when scale_auto_sync=True.
"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.scale.models_db import (
    AgentDB,
    MCPServerDB,
    OrchestrationDB,
    SettingDB,
    ToolDB,
)


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


async def sync_orchestrations_to_pg(
    session: AsyncSession,
    tenant_id: str = "default",
) -> dict:
    """Upsert all orchestrations from local JSON into Postgres."""
    try:
        from core.config import DATA_DIR
        from core.json_store import JsonStore

        store = JsonStore(os.path.join(DATA_DIR, "orchestrations.json"))
        items = store.load()
        if not isinstance(items, list):
            items = []
    except Exception as e:
        return {"synced": 0, "errors": [str(e)]}

    synced = 0
    errors = []
    for item in items:
        try:
            stmt = pg_insert(OrchestrationDB).values(
                id=item["id"],
                name=item.get("name", ""),
                description=item.get("description", ""),
                definition=item,
                tenant_id=tenant_id,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "definition": item,
                    "tenant_id": tenant_id,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(stmt)
            synced += 1
        except Exception as e:
            errors.append(f"orchestration {item.get('id', '?')}: {e}")

    await session.commit()
    return {"synced": synced, "errors": errors}


async def sync_agents_to_pg(
    session: AsyncSession,
    tenant_id: str = "default",
) -> dict:
    """Upsert all agents from local JSON into Postgres."""
    try:
        from core.config import DATA_DIR
        from core.json_store import JsonStore

        store = JsonStore(os.path.join(DATA_DIR, "user_agents.json"))
        items = store.load()
        if not isinstance(items, list):
            items = []
    except Exception as e:
        return {"synced": 0, "errors": [str(e)]}

    synced = 0
    errors = []
    for item in items:
        try:
            stmt = pg_insert(AgentDB).values(
                id=item["id"],
                name=item.get("name", ""),
                definition=item,
                tenant_id=tenant_id,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": item.get("name", ""),
                    "definition": item,
                    "tenant_id": tenant_id,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(stmt)
            synced += 1
        except Exception as e:
            errors.append(f"agent {item.get('id', '?')}: {e}")

    await session.commit()
    return {"synced": synced, "errors": errors}


async def sync_tools_to_pg(
    session: AsyncSession,
    tenant_id: str = "default",
) -> dict:
    """Upsert all custom tools from local JSON into Postgres."""
    try:
        from core.config import DATA_DIR
        from core.json_store import JsonStore

        store = JsonStore(os.path.join(DATA_DIR, "custom_tools.json"))
        items = store.load()
        if not isinstance(items, list):
            items = []
    except Exception as e:
        return {"synced": 0, "errors": [str(e)]}

    synced = 0
    errors = []
    for item in items:
        try:
            tool_id = item.get("id") or item.get("name", "")
            stmt = pg_insert(ToolDB).values(
                id=tool_id,
                name=item.get("name", ""),
                definition=item,
                tenant_id=tenant_id,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": item.get("name", ""),
                    "definition": item,
                    "tenant_id": tenant_id,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(stmt)
            synced += 1
        except Exception as e:
            errors.append(f"tool {item.get('id', '?')}: {e}")

    await session.commit()
    return {"synced": synced, "errors": errors}


# Keys from settings.json that workers need to run LLM calls.
_LLM_SETTING_KEYS = [
    "model", "mode",
    "openai_key", "anthropic_key", "gemini_key", "grok_key", "deepseek_key",
    "openai_compatible_key", "openai_compatible_base_url", "openai_compatible_models",
    "local_compatible_base_url", "local_compatible_key", "local_compatible_models",
    "openai_compatible_embed_models", "local_compatible_embed_models",
    "huggingface_token", "huggingface_models", "huggingface_max_new_tokens",
    "anthropic_cli_models", "gemini_cli_models", "codex_cli_models", "github_copilot_cli_models",
    "bedrock_api_key", "bedrock_inference_profile",
    "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "aws_region",
    "embedding_model",
    "prompt_cache_enabled", "transform_runtime",
    "vault_enabled", "vault_threshold",
    "allow_db_write",
    # S3 storage — needed by workers for vault + log uploads
    "s3_bucket", "s3_region", "s3_prefix",
    "s3_access_key_id", "s3_secret_access_key", "s3_endpoint_url",
]


async def sync_mcp_servers_to_pg(
    session: AsyncSession,
    tenant_id: str = "default",
) -> dict:
    """Upsert all MCP server configs from local JSON into Postgres.
    Sensitive fields (token) are stripped before storing."""
    try:
        from core.mcp_client import MCP_SERVERS_FILE
        with open(MCP_SERVERS_FILE) as f:
            items = json.load(f)
        if not isinstance(items, list):
            items = []
    except Exception as e:
        return {"synced": 0, "errors": [str(e)]}

    _STRIP_FIELDS = {"token", "status"}

    synced = 0
    errors = []
    for item in items:
        name = item.get("name", "")
        if not name:
            continue
        try:
            safe_def = {k: v for k, v in item.items() if k not in _STRIP_FIELDS}
            stmt = pg_insert(MCPServerDB).values(
                name=name,
                label=item.get("label", name),
                definition=safe_def,
                tenant_id=tenant_id,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "label": item.get("label", name),
                    "definition": safe_def,
                    "tenant_id": tenant_id,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(stmt)
            synced += 1
        except Exception as e:
            errors.append(f"mcp_server {name}: {e}")

    await session.commit()
    return {"synced": synced, "errors": errors}


async def sync_settings_to_pg(session: AsyncSession) -> dict:
    """Upsert relevant settings from local settings.json into Postgres."""
    try:
        from core.config import load_settings
        settings = load_settings()
    except Exception as e:
        return {"synced": 0, "errors": [str(e)]}

    synced = 0
    errors = []
    for key in _LLM_SETTING_KEYS:
        value = settings.get(key)
        if value is None:
            continue
        try:
            stmt = pg_insert(SettingDB).values(
                key=key,
                value=json.dumps(value),
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": json.dumps(value),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(stmt)
            synced += 1
        except Exception as e:
            errors.append(f"setting {key}: {e}")

    await session.commit()
    return {"synced": synced, "errors": errors}


async def full_sync(
    session: AsyncSession,
    tenant_id: str = "default",
) -> dict:
    """Run all sync operations and return combined results."""
    results = {}

    results["orchestrations"] = await sync_orchestrations_to_pg(session, tenant_id)
    results["agents"] = await sync_agents_to_pg(session, tenant_id)
    results["tools"] = await sync_tools_to_pg(session, tenant_id)
    results["mcp_servers"] = await sync_mcp_servers_to_pg(session, tenant_id)
    results["settings"] = await sync_settings_to_pg(session)

    total_synced = sum(r.get("synced", 0) for r in results.values())
    all_errors = [e for r in results.values() for e in r.get("errors", [])]

    return {
        "total_synced": total_synced,
        "errors": all_errors,
        "details": results,
        "synced_at": _now_str(),
    }


async def get_sync_status(session: AsyncSession) -> dict:
    """Return row counts per table for the sync status display."""
    from sqlalchemy import func, select

    counts = {}
    for model, label in [
        (OrchestrationDB, "orchestrations"),
        (AgentDB, "agents"),
        (ToolDB, "tools"),
        (MCPServerDB, "mcp_servers"),
        (SettingDB, "settings"),
    ]:
        result = await session.execute(select(func.count()).select_from(model))
        counts[label] = result.scalar() or 0

    return counts
