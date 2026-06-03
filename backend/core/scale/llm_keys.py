"""
Load LLM API keys from Postgres settings table and inject them into the
worker process environment. Workers call this on startup so they can make
LLM API calls without needing access to the local settings.json.
Falls back to environment variables if Postgres is unavailable.
"""
import json
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.scale.models_db import SettingDB


# Map setting keys → environment variable names that LLM providers read
_KEY_TO_ENV = {
    "openai_key": "OPENAI_API_KEY",
    "anthropic_key": "ANTHROPIC_API_KEY",
    "gemini_key": "GEMINI_API_KEY",
    "grok_key": "GROK_API_KEY",
    "deepseek_key": "DEEPSEEK_API_KEY",
    "openai_compatible_key": "OPENAI_COMPATIBLE_KEY",
    "openai_compatible_base_url": "OPENAI_COMPATIBLE_BASE_URL",
    "openai_compatible_models": "OPENAI_COMPATIBLE_MODELS",
    "local_compatible_base_url": "LOCAL_COMPATIBLE_BASE_URL",
    "local_compatible_key": "LOCAL_COMPATIBLE_KEY",
    "local_compatible_models": "LOCAL_COMPATIBLE_MODELS",
    "openai_compatible_embed_models": "OPENAI_COMPATIBLE_EMBED_MODELS",
    "local_compatible_embed_models": "LOCAL_COMPATIBLE_EMBED_MODELS",
    "huggingface_token": "HF_TOKEN",
    "aws_access_key_id": "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "aws_session_token": "AWS_SESSION_TOKEN",
    "aws_region": "AWS_DEFAULT_REGION",
    "bedrock_api_key": "BEDROCK_API_KEY",
}


async def load_llm_settings_from_pg(session: AsyncSession) -> dict:
    """Load LLM-related settings from the Postgres scale_settings table."""
    result = await session.execute(select(SettingDB))
    rows = result.scalars().all()
    settings = {}
    for row in rows:
        try:
            settings[row.key] = json.loads(row.value)
        except Exception:
            settings[row.key] = row.value
    return settings


def inject_llm_env(settings: dict) -> None:
    """Set LLM provider environment variables from loaded settings dict.
    Only overwrites if the env var is not already set (env takes precedence).
    """
    for setting_key, env_var in _KEY_TO_ENV.items():
        value = settings.get(setting_key)
        if value and not os.environ.get(env_var):
            os.environ[env_var] = str(value)

    # Also inject as the internal Synapse settings keys used by llm_providers.py
    for key, value in settings.items():
        env_key = f"SYNAPSE_SETTING_{key.upper()}"
        if value is not None and not os.environ.get(env_key):
            os.environ[env_key] = json.dumps(value)
