"""
Shared registry of native MCP tool scripts.
Single source of truth for tool filenames used by both the API server and workers.
Import this instead of hard-coding paths in server.py or worker_server_module.py.
"""
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

# All native Python-based MCP tools available in the system
ALL_NATIVE_TOOLS: dict[str, str] = {
    "time":              str(_TOOLS_DIR / "time.py"),
    "sql":               str(_TOOLS_DIR / "sql_agent.py"),
    "personal_details":  str(_TOOLS_DIR / "personal_details.py"),
    "collect_data":      str(_TOOLS_DIR / "collect_data.py"),
    "pdf_parser":        str(_TOOLS_DIR / "pdf_parser.py"),
    "xlsx_parser":       str(_TOOLS_DIR / "xlsx_parser.py"),
    "vault_sandbox":     str(_TOOLS_DIR / "sandbox.py"),
    "code_vault_search": str(_TOOLS_DIR / "code_search.py"),
    "web_scraper":       str(_TOOLS_DIR / "web_scraper.py"),
    "bash":              str(_TOOLS_DIR / "bash.py"),
    "file_reader":       str(_TOOLS_DIR / "file_reader.py"),
}

# Tools safe for headless worker processes.
# Excluded from workers: sql (needs DB config injection), personal_details (UI-only),
# code_vault_search (large index, memory-heavy in workers).
WORKER_NATIVE_TOOLS: set[str] = {
    "time",
    "collect_data",
    "pdf_parser",
    "xlsx_parser",
    "vault_sandbox",
    "web_scraper",
    "bash",
    "file_reader",
}

# npx-based MCP servers available to workers.
# Excluded: Browser Automation (requires local display), Google Workspace (OAuth session).
WORKER_NPX_TOOLS: dict[str, list[str]] = {
    "Sequential Thinking": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
}
