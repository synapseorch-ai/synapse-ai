"""
Synthetic data, models, bedrock, config, history endpoints.
"""
import os
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

from core.config import load_settings, DATA_DIR
from core.llm_providers import _make_aws_client
from core.session import session_state, _CHAT_SESSIONS_DIR
from services.synthetic_data import generate_synthetic_data, SyntheticDataRequest, current_job, DATASETS_DIR

router = APIRouter()


def _bedrock_management_get(path: str, region: str, api_key: str, params: dict | None = None) -> dict:
    """Direct HTTPS call to the Bedrock management (control-plane) API using bearer token auth.

    The Bedrock management API (list_foundation_models, list_inference_profiles) does not
    support the AWS_BEARER_TOKEN_BEDROCK env var that boto3 uses for bedrock-runtime.
    Direct HTTP with Authorization: Bearer is the correct approach for API key auth here.
    """
    import urllib.request
    import urllib.parse
    url = f"https://bedrock.{region}.amazonaws.com{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json as _json
        return _json.loads(resp.read())


# --- Synthetic Data ---

@router.post("/api/synthetic/generate")
async def start_synthetic_generation(req: SyntheticDataRequest):
    if current_job["status"] == "generating":
        raise HTTPException(status_code=400, detail="A generation job is already running.")

    asyncio.create_task(generate_synthetic_data(req))
    return {"status": "started", "message": "Generation started in background."}


@router.get("/api/synthetic/status")
async def get_synthetic_status():
    return current_job


@router.get("/api/synthetic/datasets")
async def list_datasets():
    if not os.path.exists(DATASETS_DIR):
        return []
    files = [f for f in os.listdir(DATASETS_DIR) if f.endswith(".jsonl")]
    results = []
    for f in files:
        path = os.path.join(DATASETS_DIR, f)
        stats = os.stat(path)
        results.append({
            "filename": f,
            "size": stats.st_size,
            "created": datetime.fromtimestamp(stats.st_ctime).isoformat()
        })
    return sorted(results, key=lambda x: x["created"], reverse=True)


# --- Models ---

_COPILOT_FALLBACK_MODELS = [
    "cli.copilot",
    "cli.copilot.claude-sonnet-4-5",
    "cli.copilot.claude-opus-4-5",
    "cli.copilot.claude-haiku-4-5",
    "cli.copilot.gpt-4o",
    "cli.copilot.gpt-4.1",
    "cli.copilot.o3",
    "cli.copilot.o4-mini",
]

async def _get_github_token() -> "str | None":
    """Discover a GitHub token from env vars, gh CLI, or copilot config files (cross-platform)."""
    import shutil, os as _os, json as _json, platform
    token = (_os.getenv("COPILOT_GITHUB_TOKEN")
             or _os.getenv("GH_TOKEN")
             or _os.getenv("GITHUB_TOKEN"))
    if token:
        return token
    if shutil.which("gh"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "auth", "token",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0 and (t := stdout.decode().strip()):
                return t
        except Exception:
            pass
    # Config file paths differ by OS
    system = platform.system()
    if system == "Windows":
        config_dirs = [Path(_os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "github-copilot"]
    elif system == "Darwin":
        config_dirs = [
            Path.home() / "Library" / "Application Support" / "github-copilot",
            Path.home() / ".config" / "github-copilot",
        ]
    else:  # Linux and others
        config_dirs = [
            Path(_os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "github-copilot",
        ]
    for config_dir in config_dirs:
        hosts_path = config_dir / "hosts.json"
        if hosts_path.exists():
            try:
                data = _json.loads(hosts_path.read_text())
                t = (data.get("github.com", {}).get("oauth_token")
                     or data.get("github.com", {}).get("token"))
                if t:
                    return t
            except Exception:
                pass
    # Older plaintext fallback (~/.copilot/config.json)
    config_path = Path.home() / ".copilot" / "config.json"
    if config_path.exists():
        try:
            data = _json.loads(config_path.read_text())
            if t := (data.get("oauth_token") or data.get("token")):
                return t
        except Exception:
            pass
    return None


async def _fetch_copilot_models() -> "list[str]":
    """Fetch live model list from GitHub Models catalog API; falls back to hardcoded list."""
    token = await _get_github_token()
    if not token:
        return _COPILOT_FALLBACK_MODELS
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.github.com/catalog/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if resp.status_code != 200:
            return _COPILOT_FALLBACK_MODELS
        data = resp.json()
        names = [m["name"] for m in data if isinstance(m, dict) and m.get("name")]
        return (["cli.copilot"] + [f"cli.copilot.{n}" for n in names]) if names else _COPILOT_FALLBACK_MODELS
    except Exception:
        return _COPILOT_FALLBACK_MODELS


@router.get("/api/models")
async def get_models():
    """Fetches available models dynamically from each provider's API."""
    settings = load_settings()

    # --- Fallback model lists (used when API calls fail) ---
    GEMINI_FALLBACK = ["gemini-2.5-pro-preview-05-06", "gemini-2.5-flash-preview-04-17", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash"]
    ANTHROPIC_FALLBACK = ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"]
    OPENAI_FALLBACK = ["gpt-4o", "gpt-4-turbo", "gpt-4o-mini"]
    BEDROCK_FALLBACK = ["bedrock.anthropic.claude-3-5-sonnet-20240620-v1:0", "bedrock.anthropic.claude-3-sonnet-20240229-v1:0"]
    GROK_FALLBACK = ["grok-3", "grok-3-mini", "grok-2-1212", "grok-2-vision-1212"]
    DEEPSEEK_FALLBACK = ["deepseek-chat", "deepseek-reasoner"]

    # --- Check API keys ---
    gemini_key = (settings.get("gemini_key") or "").strip()
    anthropic_key = (settings.get("anthropic_key") or "").strip()
    openai_key = (settings.get("openai_key") or "").strip()
    grok_key = (settings.get("grok_key") or "").strip()
    deepseek_key = (settings.get("deepseek_key") or "").strip()
    openai_compatible_key = (settings.get("openai_compatible_key") or "").strip()
    openai_compatible_base_url = (settings.get("openai_compatible_base_url") or "").strip()
    openai_compatible_models_str = (settings.get("openai_compatible_models") or "").strip()
    local_compatible_base_url = (settings.get("local_compatible_base_url") or "").strip()
    local_compatible_key = (settings.get("local_compatible_key") or "").strip()
    local_compatible_models_str = (settings.get("local_compatible_models") or "").strip()
    openai_compatible_embed_models_str = (settings.get("openai_compatible_embed_models") or "").strip()
    local_compatible_embed_models_str = (settings.get("local_compatible_embed_models") or "").strip()
    huggingface_models_str = (settings.get("huggingface_models") or "").strip()
    bedrock_available = bool((settings.get("bedrock_api_key") or "").strip() or
                             (settings.get("aws_access_key_id") or "").strip())

    # --- Fetch models from each provider concurrently ---
    async def fetch_ollama() -> tuple[bool, list[str], list[str]]:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')}/api/tags", timeout=3.0)
                if r.status_code == 200:
                    models = [f"ollama.{m['name']}" for m in r.json().get("models", [])]
                    # Simple heuristic: models with 'embed' in name are likely for embeddings
                    embeds = [m for m in models if "embed" in m.lower()]
                    return True, models, embeds
        except Exception:
            pass
        return False, [], []

    async def fetch_openai() -> tuple[bool, list[str], list[str]]:
        if not openai_key:
            return False, OPENAI_FALLBACK, ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    chat_models = sorted(set(
                        m["id"] for m in data
                        if m.get("id", "").startswith(("gpt-4", "gpt-3.5"))
                        and "instruct" not in m.get("id", "")
                    ), reverse=True)
                    embed_models = sorted(set(
                        m["id"] for m in data if "embedding" in m.get("id", "")
                    ))
                    return True, chat_models if chat_models else OPENAI_FALLBACK, embed_models if embed_models else ["text-embedding-3-small", "text-embedding-3-large"]
                else:
                    print(f"Error fetching OpenAI models: HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Error fetching OpenAI models: {type(e).__name__}: {e}")
        return True, OPENAI_FALLBACK, ["text-embedding-3-small", "text-embedding-3-large"]

    async def fetch_anthropic() -> tuple[bool, list[str], list[str]]:
        if not anthropic_key:
            return False, ANTHROPIC_FALLBACK, []
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": anthropic_key,
                        "anthropic-version": "2023-06-01",
                    },
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    models = sorted(set(m["id"] for m in data if m.get("id")), reverse=True)
                    return True, models if models else ANTHROPIC_FALLBACK, []
                else:
                    print(f"Error fetching Anthropic models: HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Error fetching Anthropic models: {type(e).__name__}: {e}")
        return True, ANTHROPIC_FALLBACK, []

    async def fetch_gemini() -> tuple[bool, list[str], list[str]]:
        if not gemini_key:
            return False, GEMINI_FALLBACK, ["text-embedding-004", "gemini-embedding-001"]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}",
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json().get("models", [])
                    chat_models = []
                    embed_models = []
                    for m in data:
                        name = m.get("name", "").replace("models/", "")
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in methods:
                            chat_models.append(name)
                        if "embedContent" in methods:
                            embed_models.append(name)
                    return True, sorted(set(chat_models)) if chat_models else GEMINI_FALLBACK, sorted(set(embed_models)) if embed_models else ["text-embedding-004"]
                else:
                    print(f"Error fetching Gemini models: HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Error fetching Gemini models: {type(e).__name__}: {e}")
        return True, GEMINI_FALLBACK, ["text-embedding-004"]

    async def fetch_grok() -> tuple[bool, list[str], list[str]]:
        if not grok_key:
            return False, GROK_FALLBACK, []
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.x.ai/v1/models",
                    headers={"Authorization": f"Bearer {grok_key}"},
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    models = sorted(set(
                        m["id"] for m in data if m.get("id", "").startswith("grok")
                    ), reverse=True)
                    return True, models if models else GROK_FALLBACK, []
                else:
                    print(f"Error fetching Grok models: HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Error fetching Grok models: {type(e).__name__}: {e}")
        return True, GROK_FALLBACK, []

    async def fetch_deepseek() -> tuple[bool, list[str], list[str]]:
        if not deepseek_key:
            return False, DEEPSEEK_FALLBACK, []
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.deepseek.com/v1/models",
                    headers={"Authorization": f"Bearer {deepseek_key}"},
                    timeout=5.0,
                )
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    models = sorted(set(
                        m["id"] for m in data if m.get("id", "").startswith("deepseek")
                    ), reverse=True)
                    return True, models if models else DEEPSEEK_FALLBACK, []
                else:
                    print(f"Error fetching DeepSeek models: HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Error fetching DeepSeek models: {type(e).__name__}: {e}")
        return True, DEEPSEEK_FALLBACK, []

    async def fetch_bedrock() -> tuple[bool, list[str], list[str]]:
        if not bedrock_available:
            return False, BEDROCK_FALLBACK, ["amazon.titan-embed-text-v1", "amazon.titan-embed-text-v2:0"]
        try:
            region = (settings.get("aws_region") or "us-east-1").strip() or "us-east-1"
            api_key = (settings.get("bedrock_api_key") or "").strip()

            def _list():
                summaries = []
                if api_key:
                    # API key auth: use direct HTTPS — boto3 does not apply bearer tokens
                    # to the Bedrock management (control-plane) API.
                    data = _bedrock_management_get("/foundation-models", region, api_key)
                    summaries = data.get("modelSummaries", []) or []
                else:
                    c = _make_aws_client("bedrock", region, settings)
                    resp = c.list_foundation_models()
                    summaries = resp.get("modelSummaries", []) or []
                chat = []
                embed = []
                for m in summaries:
                    mid = f"bedrock.{m['modelId']}"
                    if "EMBEDDING" in m.get("outputModalities", []):
                        embed.append(mid)
                    else:
                        chat.append(mid)
                return chat, embed

            chat, embed = await asyncio.to_thread(_list)
            return True, chat if chat else BEDROCK_FALLBACK, embed if embed else ["bedrock.amazon.titan-embed-text-v1"]
        except Exception:
            return True, BEDROCK_FALLBACK, ["bedrock.amazon.titan-embed-text-v1"]

    async def fetch_claude_cli() -> tuple[bool, list[str], list[str]]:
        import shutil
        models = [
            "cli.claude.claude-sonnet-4-6",
            "cli.claude.claude-sonnet-4-6-thinking",
            "cli.claude.claude-opus-4-6",
            "cli.claude.claude-opus-4-6-thinking",
            "cli.claude.claude-haiku-4-5-20251001",
            "cli.claude.claude-sonnet-4-5-20250929",
            "cli.claude.claude-opus-4-5-20251101",
            "cli.claude.claude-opus-4-5-20251101-thinking",
            "cli.claude"
        ] if shutil.which("claude") else []
        return bool(models), models, []

    async def fetch_gemini_cli() -> tuple[bool, list[str], list[str]]:
        import shutil
        models = [
            "cli.gemini.pro",
            "cli.gemini.flash",
            "cli.gemini"
        ] if shutil.which("gemini") else []
        return bool(models), models, []

    async def fetch_codex_cli() -> tuple[bool, list[str], list[str]]:
        import shutil
        models = ["cli.codex"] if shutil.which("codex") else []
        return bool(models), models, []

    async def fetch_github_copilot_cli() -> tuple[bool, list[str], list[str]]:
        import shutil
        if not shutil.which("copilot"):
            return False, [], []
        # Verify it's the GitHub Copilot CLI binary
        try:
            proc = await asyncio.create_subprocess_exec(
                "copilot", "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode != 0:
                return False, [], []
        except Exception:
            return False, [], []
        models = await _fetch_copilot_models()
        return True, models, []

    async def fetch_openai_compatible() -> tuple[bool, list[str], list[str]]:
        if not openai_compatible_base_url:
            return False, [], []
        models = [m.strip() for m in openai_compatible_models_str.split(",") if m.strip()]
        manual_embed = [m.strip() for m in openai_compatible_embed_models_str.split(",") if m.strip()]
        if openai_compatible_key and openai_compatible_base_url:
            try:
                url = openai_compatible_base_url.rstrip("/")
                if url.endswith("/v1"):
                    url = url[:-3]
                async with httpx.AsyncClient() as client:
                    headers = {}
                    if openai_compatible_key:
                        headers["Authorization"] = f"Bearer {openai_compatible_key}"
                    r = await client.get(f"{url}/v1/models", headers=headers, timeout=5.0)
                    if r.status_code == 200:
                        data = r.json().get("data", [])
                        api_models = sorted(set(m["id"] for m in data if m.get("id")))
                        if api_models:
                            models = api_models
            except Exception as e:
                print(f"Error fetching OpenAI-compatible models: {type(e).__name__}: {e}")
        available = bool(openai_compatible_base_url)
        prefixed = [f"oaic.{m}" for m in models]
        auto_embed = [m for m in models if "embed" in m.lower()]
        all_embed = list(dict.fromkeys(manual_embed + auto_embed))
        prefixed_embed = [f"oaic.{m}" for m in all_embed]
        return available, prefixed, prefixed_embed

    async def fetch_local_compatible() -> tuple[bool, list[str], list[str]]:
        if not local_compatible_base_url:
            return False, [], []
        models = [m.strip() for m in local_compatible_models_str.split(",") if m.strip()]
        manual_embed = [m.strip() for m in local_compatible_embed_models_str.split(",") if m.strip()]
        try:
            url = local_compatible_base_url.rstrip("/")
            if url.endswith("/v1"):
                url = url[:-3]
            async with httpx.AsyncClient() as client:
                headers = {}
                if local_compatible_key:
                    headers["Authorization"] = f"Bearer {local_compatible_key}"
                r = await client.get(f"{url}/v1/models", headers=headers, timeout=5.0)
                if r.status_code == 200:
                    data = r.json().get("data", [])
                    api_models = sorted(set(m["id"] for m in data if m.get("id")))
                    if api_models:
                        models = api_models
        except Exception as e:
            print(f"Error fetching local-compatible models: {type(e).__name__}: {e}")
        available = bool(local_compatible_base_url)
        prefixed = [f"locv1.{m}" for m in models]
        auto_embed = [m for m in models if "embed" in m.lower()]
        all_embed = list(dict.fromkeys(manual_embed + auto_embed))
        prefixed_embed = [f"locv1.{m}" for m in all_embed]
        return available, prefixed, prefixed_embed

    async def fetch_huggingface() -> tuple[bool, list[str], list[str]]:
        """List user-configured HuggingFace model IDs, prefixed with hf.

        No API discovery — HF model namespace is enormous and not enumerable.
        The user lists the specific model IDs they want available (one per line
        or comma-separated) in Settings → Models → HuggingFace Models.
        """
        models = [
            m.strip() for m in huggingface_models_str.replace("\n", ",").split(",")
            if m.strip()
        ]
        if not models:
            return False, [], []
        prefixed = [f"hf.{m}" if not m.startswith("hf.") else m for m in models]
        return True, prefixed, []

    # Run all fetches concurrently; return_exceptions=True ensures one provider failure
    # doesn't cancel the others.
    _PROVIDER_FALLBACKS = [
        (False, [], []),                                                                 # ollama
        (True, OPENAI_FALLBACK, ["text-embedding-3-small", "text-embedding-3-large"]),  # openai
        (True, ANTHROPIC_FALLBACK, []),                                                  # anthropic
        (True, GEMINI_FALLBACK, ["text-embedding-004"]),                                 # gemini
        (True, GROK_FALLBACK, []),                                                       # grok
        (True, DEEPSEEK_FALLBACK, []),                                                   # deepseek
        (True, BEDROCK_FALLBACK, ["bedrock.amazon.titan-embed-text-v1"]),                # bedrock
        (False, [], []),                                                                 # openai_compatible
        (False, [], []),                                                                 # local_compatible
        (False, [], []),                                                                 # anthropic_cli
        (False, [], []),                                                                 # gemini_cli
        (False, [], []),                                                                 # codex_cli
        (False, [], []),                                                                 # github_copilot_cli
        (False, [], []),                                                                 # huggingface
    ]
    _PROVIDER_NAMES = ["ollama", "openai", "anthropic", "gemini", "grok", "deepseek", "bedrock", "openai_compatible", "local_compatible", "anthropic_cli", "gemini_cli", "codex_cli", "github_copilot_cli", "huggingface"]

    raw = await asyncio.gather(
        fetch_ollama(), fetch_openai(), fetch_anthropic(),
        fetch_gemini(), fetch_grok(), fetch_deepseek(), fetch_bedrock(),
        fetch_openai_compatible(), fetch_local_compatible(),
        fetch_claude_cli(), fetch_gemini_cli(), fetch_codex_cli(),
        fetch_github_copilot_cli(),
        fetch_huggingface(),
        return_exceptions=True,
    )

    results = []
    for i, r in enumerate(raw):
        if isinstance(r, BaseException):
            print(f"Error fetching {_PROVIDER_NAMES[i]} models: {type(r).__name__}: {r}")
            results.append(_PROVIDER_FALLBACKS[i])
        else:
            results.append(r)

    ollama_avail, ollama_chat, ollama_embed = results[0]
    openai_avail, openai_chat, openai_embed = results[1]
    anthropic_avail, anthropic_chat, anthropic_embed = results[2]
    gemini_avail, gemini_chat, gemini_embed = results[3]
    grok_avail, grok_chat, grok_embed = results[4]
    deepseek_avail, deepseek_chat, deepseek_embed = results[5]
    bedrock_avail, bedrock_chat, bedrock_embed = results[6]
    oaic_avail, oaic_chat, oaic_embed = results[7]
    locv1_avail, locv1_chat, locv1_embed = results[8]
    c_claude_avail, c_claude_chat, _ = results[9]
    c_gemini_avail, c_gemini_chat, _ = results[10]
    c_codex_avail, c_codex_chat, _ = results[11]
    c_copilot_avail, c_copilot_chat, _ = results[12]
    hf_avail, hf_chat, _ = results[13]

    # --- Build provider map ---
    providers = {
        "ollama": {"available": ollama_avail, "models": ollama_chat, "embedding_models": ollama_embed},
        "gemini": {"available": gemini_avail, "models": gemini_chat, "embedding_models": gemini_embed},
        "anthropic": {"available": anthropic_avail, "models": anthropic_chat, "embedding_models": anthropic_embed},
        "openai": {"available": openai_avail, "models": openai_chat, "embedding_models": openai_embed},
        "grok": {"available": grok_avail, "models": grok_chat, "embedding_models": grok_embed},
        "deepseek": {"available": deepseek_avail, "models": deepseek_chat, "embedding_models": deepseek_embed},
        "bedrock": {"available": bedrock_avail, "models": bedrock_chat, "embedding_models": bedrock_embed},
        "openai_compatible": {"available": oaic_avail, "models": oaic_chat, "embedding_models": oaic_embed},
        "local_compatible": {"available": locv1_avail, "models": locv1_chat, "embedding_models": locv1_embed},
        "anthropic_cli": {"available": c_claude_avail, "models": c_claude_chat, "embedding_models": []},
        "gemini_cli": {"available": c_gemini_avail, "models": c_gemini_chat, "embedding_models": []},
        "codex_cli": {"available": c_codex_avail, "models": c_codex_chat, "embedding_models": []},
        "github_copilot_cli": {"available": c_copilot_avail, "models": c_copilot_chat, "embedding_models": []},
        "huggingface": {"available": hf_avail, "models": hf_chat, "embedding_models": []},
    }

    # --- Flat list of all available models ---
    all_available = []
    for _, info in providers.items():
        if info["available"]:
            all_available.extend(info["models"])

    # --- Backward compat ---
    cloud_models = gemini_chat + anthropic_chat + openai_chat + grok_chat + deepseek_chat + BEDROCK_FALLBACK + oaic_chat + locv1_chat + c_claude_chat + c_gemini_chat + c_codex_chat + c_copilot_chat + hf_chat

    return {
        "providers": providers,
        "all_available": all_available,
        "local": ollama_chat,
        "cloud": cloud_models,
    }




@router.get("/api/bedrock/models")
async def get_bedrock_models():
    """Lists Bedrock foundation models."""
    settings = load_settings()
    region = (settings.get("aws_region") or "us-east-1").strip() or "us-east-1"
    api_key = (settings.get("bedrock_api_key") or "").strip()

    def _list_models_sync():
        summaries = []
        if api_key:
            # API key (ABSK / bedrock-api-key format): use direct HTTPS bearer auth.
            # The Bedrock management API does not support AWS_BEARER_TOKEN_BEDROCK via boto3.
            data = _bedrock_management_get("/foundation-models", region, api_key)
            summaries = data.get("modelSummaries", []) or []
        else:
            client = _make_aws_client("bedrock", region, settings)
            resp = client.list_foundation_models()
            summaries = resp.get("modelSummaries", []) or []
        models: list[str] = []
        for s in summaries:
            model_id = s.get("modelId")
            if model_id:
                models.append(f"bedrock.{model_id}")
        return sorted(set(models))

    try:
        models = await asyncio.to_thread(_list_models_sync)
        return {"models": models}
    except Exception as e:
        print(f"Error listing Bedrock models: {type(e).__name__}: {e}")
        return {
            "models": [],
            "error": f"Unable to list Bedrock models: {e}",
        }


@router.get("/api/bedrock/inference-profiles")
async def get_bedrock_inference_profiles():
    """Lists Bedrock inference profiles."""
    settings = load_settings()
    region = (settings.get("aws_region") or "us-east-1").strip() or "us-east-1"
    api_key = (settings.get("bedrock_api_key") or "").strip()

    def _parse_profile_summaries(summaries) -> list[dict]:
        profiles = []
        for s in summaries or []:
            if not isinstance(s, dict):
                continue
            profiles.append(
                {
                    "id": s.get("inferenceProfileId") or s.get("id") or "",
                    "arn": s.get("inferenceProfileArn") or s.get("arn") or "",
                    "name": s.get("inferenceProfileName") or s.get("name") or "",
                    "status": s.get("status") or "",
                    "type": s.get("type") or "",
                }
            )
        return profiles

    def _list_profiles_sync():
        profiles = []
        next_token = None

        if api_key:
            # API key (ABSK / bedrock-api-key format): use direct HTTPS bearer auth.
            # The Bedrock management API does not support AWS_BEARER_TOKEN_BEDROCK via boto3.
            while True:
                params = {"nextToken": next_token} if next_token else None
                resp = _bedrock_management_get("/inference-profiles", region, api_key, params)
                print(f"Fetched {len(resp.get('inferenceProfileSummaries', []))} profiles from Bedrock")
                profiles.extend(_parse_profile_summaries(resp.get("inferenceProfileSummaries")))
                next_token = resp.get("nextToken")
                if not next_token:
                    break
        else:
            client = _make_aws_client("bedrock", region, settings)
            while True:
                kwargs: dict = {}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = client.list_inference_profiles(**kwargs)
                print(f"Fetched {len(resp.get('inferenceProfileSummaries', []))} profiles from Bedrock")
                profiles.extend(_parse_profile_summaries(resp.get("inferenceProfileSummaries")))
                next_token = resp.get("nextToken")
                if not next_token:
                    break

        return sorted(profiles, key=lambda p: (p.get("name") or p.get("arn") or p.get("id") or ""))

    try:
        profiles = await asyncio.to_thread(_list_profiles_sync)
        return {"profiles": profiles}
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"Error listing Bedrock inference profiles: {error_msg}")
        return {
            "profiles": [],
            "error": error_msg,
        }


# --- History Management ---

@router.delete("/api/history/recent")
async def clear_recent_history():
    """Clears all persisted JSON session files and in-memory session state."""
    import shutil
    import os
    session_state.clear()
    # Delete all JSON session files
    cleared = 0
    if os.path.isdir(_CHAT_SESSIONS_DIR):
        for fname in os.listdir(_CHAT_SESSIONS_DIR):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(_CHAT_SESSIONS_DIR, fname))
                    cleared += 1
                except Exception:
                    pass
    return {"status": "success", "message": f"Cleared {cleared} session file(s) and in-memory state."}


@router.delete("/api/history/all")
async def clear_all_history():
    """Clears ALL session files AND long-term ChromaDB memory (report RAG)."""
    import os
    import core.server as _server

    session_state.clear()
    # Delete all JSON session files
    cleared = 0
    if os.path.isdir(_CHAT_SESSIONS_DIR):
        for fname in os.listdir(_CHAT_SESSIONS_DIR):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(_CHAT_SESSIONS_DIR, fname))
                    cleared += 1
                except Exception:
                    pass
    # Clear ChromaDB report-RAG collections
    if _server.memory_store:
        success = _server.memory_store.clear_memory()
        if not success:
            raise HTTPException(status_code=500, detail="Failed to clear long-term memory.")
    return {"status": "success", "message": f"Cleared {cleared} session file(s) and long-term memory."}


# --- Bulk Memory Clear ---

class MemoryClearRequest(BaseModel):
    items: List[Literal["chat_history", "orchestration_history", "agent_logs", "usage", "repos", "db_configs"]]


@router.post("/api/memory/clear")
async def clear_memory_items(req: MemoryClearRequest):
    """Clear selected memory/data categories in one request."""
    from core.usage_tracker import clear_usage_logs

    items = set(req.items)
    results = {}

    # Chat sessions
    if "chat_history" in items:
        session_state.clear()
        cleared = 0
        if os.path.isdir(_CHAT_SESSIONS_DIR):
            for fname in os.listdir(_CHAT_SESSIONS_DIR):
                if fname.endswith(".json"):
                    try:
                        os.remove(os.path.join(_CHAT_SESSIONS_DIR, fname))
                        cleared += 1
                    except Exception:
                        pass
        results["chat_history"] = f"Cleared {cleared} session(s)"

    # Orchestration run history
    if "orchestration_history" in items:
        count = 0
        for logs_dir in [
            Path(__file__).parent.parent.parent / "logs" / "orchestration_runs",
            Path(__file__).parent.parent.parent / "logs" / "orchestration_logs",
        ]:
            if logs_dir.is_dir():
                for f in logs_dir.iterdir():
                    if f.is_file():
                        try:
                            f.unlink()
                            count += 1
                        except Exception:
                            pass
        results["orchestration_history"] = f"Cleared {count} file(s)"

    # Agent run logs
    if "agent_logs" in items:
        count = 0
        agent_logs_dir = Path(__file__).parent.parent.parent / "logs" / "agent_logs"
        if agent_logs_dir.is_dir():
            for f in agent_logs_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except Exception:
                        pass
        results["agent_logs"] = f"Cleared {count} file(s)"

    # Usage logs
    if "usage" in items:
        count = clear_usage_logs()
        results["usage"] = f"Cleared {count} usage record(s)"

    # Repositories
    if "repos" in items:
        repos_path = os.path.join(DATA_DIR, "repos.json")
        repos = []
        if os.path.exists(repos_path):
            try:
                with open(repos_path) as f:
                    repos = json.load(f)
            except Exception:
                pass
        for repo in repos:
            try:
                from services.code_indexer import drop_index
                drop_index(repo["id"])
            except Exception as e:
                print(f"Error dropping index for repo {repo.get('id')}: {e}")
        with open(repos_path, "w") as f:
            json.dump([], f)
        results["repos"] = f"Cleared {len(repos)} repo(s)"

    # Database configurations
    if "db_configs" in items:
        configs_path = os.path.join(DATA_DIR, "db_configs.json")
        configs = []
        if os.path.exists(configs_path):
            try:
                with open(configs_path) as f:
                    configs = json.load(f)
            except Exception:
                pass
        with open(configs_path, "w") as f:
            json.dump([], f)
        results["db_configs"] = f"Cleared {len(configs)} config(s)"

    return {"status": "success", "cleared": results}
