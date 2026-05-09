"""Lightweight interactive setup wizard for Synapse.

This writes `settings.json` into the data directory and asks for a
few common configuration options. It's intentionally small so it
works when the package is installed via `pip`.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent  # project root (synapse-ai/)
DEFAULT_DATA_DIR = Path.home() / ".synapse" / "data"

_raw_data_dir = os.getenv("SYNAPSE_DATA_DIR", str(DEFAULT_DATA_DIR))
if not os.path.isabs(_raw_data_dir):
    DATA_DIR = (ROOT_DIR / _raw_data_dir).resolve()
else:
    DATA_DIR = Path(_raw_data_dir).resolve()

SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "agent_name": "Synapse",
    "model": "",
    "mode": "cloud",
    "openai_key": "",
    "anthropic_key": "",
    "gemini_key": "",
    "google_maps_api_key": "",
    "login_enabled": False,
    "login_username": "admin",
    "login_password_hash": "",
    "bedrock_api_key": "",
    "bedrock_inference_profile": "",
    "embedding_model": "",
    "aws_access_key_id": "",
    "aws_secret_access_key": "",
    "aws_session_token": "",
    "aws_region": "us-east-1",
    "sql_connection_string": "",
    "ollama_base_url": "",
    "openai_compatible_key": "",
    "openai_compatible_base_url": "",
    "openai_compatible_models": "",
    "local_compatible_base_url": "",
    "local_compatible_key": "",
    "local_compatible_models": "",
    "openai_compatible_embed_models": "",
    "local_compatible_embed_models": "",
    "n8n_url": "http://localhost:5678",
    "n8n_api_key": "",
    "n8n_table_id": "",
    "global_config": {},
    "vault_enabled": True,
    "vault_threshold": 100000,
    "coding_agent_enabled": True,
    "report_agent_enabled": True,
    "backend_port": 8765,
    "frontend_port": 3000,
}


def _ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def _ask_yn(prompt, default="n"):
    hint = "(Y/n)" if default.lower() == "y" else "(y/N)"
    val = _ask(f"{prompt} {hint}", default).lower()
    return val in ("y", "yes")


def _ask_choice(prompt, options):
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = _ask(prompt)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(f"Enter a number between 1 and {len(options)}.")


def _fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_ollama_models(base_url="http://127.0.0.1:11434"):
    data = _fetch_json(f"{base_url}/api/tags")
    if data:
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    return []


def _fetch_gemini_models(key):
    data = _fetch_json(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}")
    if not data:
        return []
    return sorted(
        m["name"].replace("models/", "") for m in data.get("models", [])
        if m.get("name", "").startswith("models/")
        and "generateContent" in m.get("supportedGenerationMethods", [])
    )


def _fetch_openai_models(key):
    data = _fetch_json("https://api.openai.com/v1/models",
                       headers={"Authorization": f"Bearer {key}"})
    if not data:
        return []
    return sorted(set(
        m["id"] for m in data.get("data", [])
        if m.get("id") and any(p in m["id"] for p in ("gpt-4", "gpt-3.5", "o1", "o3"))
    ))


def _fetch_anthropic_models(key):
    data = _fetch_json("https://api.anthropic.com/v1/models",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if not data:
        return []
    return sorted(set(m["id"] for m in data.get("data", []) if m.get("id")), reverse=True)


def load_settings():
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **saved}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=4)


def run():
    print("\nSynapse — Interactive Setup")
    cfg = load_settings()

    print("\nGeneral")
    cfg["agent_name"] = _ask("Agent name", cfg.get("agent_name", "Synapse"))

    print("\nLLM Provider")
    providers = ["Ollama (local)", "Gemini", "OpenAI", "Claude (Anthropic)", "OpenAI Compatible", "Local V1 Compatible", "Bedrock (AWS)", "Skip for now"]
    choice = _ask_choice("Select provider", providers)
    if choice.startswith("Ollama"):
        cfg["mode"] = "local"
        cfg["ollama_base_url"] = _ask("Ollama base URL", cfg.get("ollama_base_url", "http://127.0.0.1:11434"))
        print("  Fetching Ollama models...")
        models = _fetch_ollama_models(cfg["ollama_base_url"])
        if models:
            cfg["model"] = _ask_choice("Select model", models)
        else:
            print("  No models found.")
            cfg["model"] = _ask("Model name (e.g. mistral, llama3)", cfg.get("model", "mistral"))
    elif choice == "Gemini":
        cfg["mode"] = "cloud"
        cfg["gemini_key"] = _ask("Gemini API key", cfg.get("gemini_key", ""))
        if cfg["gemini_key"]:
            print("  Fetching available models...")
            models = _fetch_gemini_models(cfg["gemini_key"])
            if models:
                cfg["model"] = _ask_choice("Select model", models)
            else:
                print("  Could not fetch models. Check your key.")
                cfg["model"] = _ask("Model name", "gemini-2.0-flash")
    elif choice == "OpenAI":
        cfg["mode"] = "cloud"
        cfg["openai_key"] = _ask("OpenAI API key", cfg.get("openai_key", ""))
        if cfg["openai_key"]:
            print("  Fetching available models...")
            models = _fetch_openai_models(cfg["openai_key"])
            if models:
                cfg["model"] = _ask_choice("Select model", models)
            else:
                print("  Could not fetch models. Check your key.")
                cfg["model"] = _ask("Model name", "gpt-4o")
    elif choice == "Claude (Anthropic)":
        cfg["mode"] = "cloud"
        cfg["anthropic_key"] = _ask("Anthropic API key", cfg.get("anthropic_key", ""))
        if cfg["anthropic_key"]:
            print("  Fetching available models...")
            models = _fetch_anthropic_models(cfg["anthropic_key"])
            if models:
                cfg["model"] = _ask_choice("Select model", models)
            else:
                print("  Could not fetch models. Check your key.")
                cfg["model"] = _ask("Model name", "claude-sonnet-4-6")
    elif choice == "Bedrock (AWS)":
        cfg["mode"] = "cloud"
        cfg["bedrock_api_key"] = _ask("Bedrock API key", cfg.get("bedrock_api_key", ""))
        cfg["aws_region"] = _ask("AWS region", cfg.get("aws_region", "us-east-1"))
    elif choice == "OpenAI Compatible":
        cfg["mode"] = "cloud"
        cfg["openai_compatible_key"] = _ask("API key", cfg.get("openai_compatible_key", ""))
        cfg["openai_compatible_base_url"] = _ask("Base URL (without /v1)", cfg.get("openai_compatible_base_url", ""))
        if cfg["openai_compatible_base_url"]:
            print("  Fetching available models...")
            models = _fetch_json(
                cfg["openai_compatible_base_url"].rstrip("/") + "/v1/models",
                headers={"Authorization": f"Bearer {cfg['openai_compatible_key']}"},
            )
            models = [m["id"] for m in (models or {}).get("data", []) if m.get("id")]
        else:
            models = []
        if models:
            chosen = _ask_choice("Select model", models)
            cfg["openai_compatible_models"] = chosen
            cfg["model"] = f"oaic.{chosen}"
        else:
            cfg["openai_compatible_models"] = _ask("Model names (comma-separated)", cfg.get("openai_compatible_models", ""))
            if cfg["openai_compatible_models"] and not cfg.get("model"):
                first = cfg["openai_compatible_models"].split(",")[0].strip()
                cfg["model"] = f"oaic.{first}"
    elif choice == "Local V1 Compatible":
        cfg["mode"] = "cloud"
        cfg["local_compatible_base_url"] = _ask("Base URL (without /v1)", cfg.get("local_compatible_base_url", ""))
        cfg["local_compatible_key"] = _ask("API key (optional)", cfg.get("local_compatible_key", ""))
        if cfg["local_compatible_base_url"]:
            print("  Fetching available models...")
            hdrs = {}
            if cfg["local_compatible_key"]:
                hdrs["Authorization"] = f"Bearer {cfg['local_compatible_key']}"
            models = _fetch_json(
                cfg["local_compatible_base_url"].rstrip("/") + "/v1/models",
                headers=hdrs,
            )
            models = [m["id"] for m in (models or {}).get("data", []) if m.get("id")]
        else:
            models = []
        if models:
            chosen = _ask_choice("Select model", models)
            cfg["local_compatible_models"] = chosen
            cfg["model"] = f"locv1.{chosen}"
        else:
            cfg["local_compatible_models"] = _ask("Model names (comma-separated)", cfg.get("local_compatible_models", ""))
            if cfg["local_compatible_models"] and not cfg.get("model"):
                first = cfg["local_compatible_models"].split(",")[0].strip()
                cfg["model"] = f"locv1.{first}"

    print("\nPorts (press Enter to keep defaults)")
    default_backend = cfg.get("backend_port", 8765)
    default_frontend = cfg.get("frontend_port", 3000)
    backend_port_str = _ask("Backend port", str(default_backend))
    frontend_port_str = _ask("Frontend (UI) port", str(default_frontend))
    try:
        cfg["backend_port"] = int(backend_port_str)
    except ValueError:
        cfg["backend_port"] = default_backend
    try:
        cfg["frontend_port"] = int(frontend_port_str)
    except ValueError:
        cfg["frontend_port"] = default_frontend

    save_settings(cfg)
    print(f"\nSettings saved to {SETTINGS_FILE}")
    print("You can reconfigure anytime with: synapse setup")


if __name__ == "__main__":
    run()
