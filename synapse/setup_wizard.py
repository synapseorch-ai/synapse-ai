"""Lightweight interactive setup wizard for Synapse.

Writes configuration into the engine's database and asks for a few common
options. It's intentionally small so it works when the package is installed
via `pip`.

Settings are rows, not a file — there is no settings.json for the wizard to
leave behind and nothing to import on first boot. `synapse/_store.py` is the
one place that knows how to reach the database from a process that is not the
backend.
"""
from pathlib import Path
import sys

from synapse import _store

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent  # project root (synapse-ai/)

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
    "global_config": {},
    "vault_enabled": True,
    "vault_threshold": 100000,
    "coding_agent_enabled": True,
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


def _ask_provider() -> dict:
    """The installer's own question (ONB1, D295, F724): keys for one or more
    providers, each checked by listing its models, then the everyday model and
    Synapse AI's. One question for both ways in, so a provider added to the
    installer is offered here too. {} when skipped, or with nobody at a keyboard."""
    from synapse.installer import providers, screen, tui
    from synapse.installer.widgets import Asker

    term = tui.Term()
    s = screen.make_screen(term, "Synapse setup")
    s.open()
    try:
        answer = providers.ask(Asker(s, term.keys()))
    finally:
        s.close()
        term.close()
    if answer:
        print(f"  {providers.describe(answer)}")
    return answer


def load_settings():
    return {**DEFAULT_SETTINGS, **_store.load_settings()}


def save_settings(cfg):
    _store.save_settings(cfg)


def run():
    # Best-effort: ensure console output uses UTF-8 so stray non-ASCII
    # characters never crash or render as garbage on Windows (cp1252).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("\nSynapse - Interactive Setup")
    cfg = load_settings()

    print("\nGeneral")
    cfg["agent_name"] = _ask("Agent name", cfg.get("agent_name", "Synapse"))

    print("\nAI provider")
    cfg.update(_ask_provider())

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
    print("\nSettings saved.")
    print("You can reconfigure anytime with: synapse setup")


if __name__ == "__main__":
    run()
