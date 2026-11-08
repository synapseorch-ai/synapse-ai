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
    "vault":             str(_TOOLS_DIR / "vault_fs.py"),
}

# Tools whose *subprocess* resolves tenant state for itself — the vault, the
# store, or the tenant's settings — rather than being handed what it needs.
#
# A ContextVar does not cross a process boundary, so one of these spawned once
# and shared reads the default tenant's data whoever calls it. They are
# therefore spawned per tenant, with `core/tool_server.py` telling each which
# tenant it serves. Everything not listed here advertises the same behaviour to
# everybody and is shared process-wide.
#
# `tests/install/test_tenant_scoped_tools.py` derives this set from the imports
# and fails if a tool starts reading tenant state without joining it.
TENANT_SCOPED_TOOLS: set[str] = {
    "bash",
    "code_vault_search",
    "file_reader",
    "sql",
    "vault",
    "vault_sandbox",
}

# Tools safe for headless worker processes.
# Excluded from workers: personal_details (UI-only), code_vault_search (large
# index, memory-heavy in workers).
#
# `sql` was excluded too, with the reason "needs DB config injection". That was
# true when a worker had no way to know whose database configs to read — and it
# stopped being true in the tenancy refactor: `tools/sql_agent.py::_load_db_configs`
# reads `collections.load("db_configs")` through the store, `sql` is in
# TENANT_SCOPED_TOOLS below, and `core/tool_server.py::bootstrap` tells each
# subprocess which tenant it serves. Nothing is injected; it resolves its own.
#
# The cost is the one `core/scale/worker_server_module.py` names: one more
# subprocess per tool per live pool entry.
WORKER_NATIVE_TOOLS: set[str] = {
    "time",
    # The vault, addressed by key through the blob store — the replacement for
    # the Node Filesystem server, which needed a directory and an `npx` no
    # scale-mode image has. See `tools/vault_fs.py` and `WORKER_NPX_TOOLS`.
    "vault",
    "collect_data",
    "pdf_parser",
    "xlsx_parser",
    "vault_sandbox",
    "web_scraper",
    "bash",
    "file_reader",
    "sql",
}

# npx-based MCP servers available to workers. **Deliberately empty.**
#
# `Sequential Thinking` lived here and never once started in a worker: the
# scale-mode images ship a Python runtime and no Node, so every tenant's module
# build spent ~7 seconds raising `FileNotFoundError: 'npx'` for a binary that
# does not exist. That line is what diagnosed a CPU-starved fleet, because one
# failed `execvp` with no I/O in the path should take microseconds.
#
# Installing Node would not have settled it. These are invoked as `npx -y <pkg>`,
# which resolves and fetches from the npm registry **at spawn time** — a network
# round trip per tenant per build, on the fleet that terminates customer
# sessions, against a version nobody pinned. Making that safe means baking
# pinned packages into the image, which is a deliberate catalogue decision and
# not something to acquire as a side effect of a default.
#
# So the worker path spawns no npx at all. The Filesystem server that used to be
# spawned alongside these is replaced by the native `vault` server in
# `WORKER_NATIVE_TOOLS`, which reaches the same files through the blob store and
# therefore works on S3 as well as on disk.
#
# **`core/server.py` is unaffected** and keeps its own npx servers — that is the
# single-process product, installed by an operator who has Node.
#
# Left as an empty dict rather than deleted: it is the seam where a deployment
# that *does* have Node can put one back, and the argument above is what such a
# deployment has to answer first.
WORKER_NPX_TOOLS: dict[str, list[str]] = {}
