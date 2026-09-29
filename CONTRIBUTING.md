# Contributing to Synapse

## Development Setup

**Prerequisites:** Python 3.11+, Node.js 18+, [Ollama](https://ollama.com/) (optional)

```bash
git clone https://github.com/synapseorch-ai/synapse-ai
cd synapse-ai
bash setup.sh      # installs all dependencies
bash start.sh      # starts backend (port 8765) + frontend (port 3000)
```

Or manually:

```bash
# Backend
cd backend
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3.11 main.py

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

## Architecture

```
frontend/                          Next.js 14 — chat UI, agent builder, orchestration canvas
  next.config.ts                   Rewrites /api/* and /auth/* to backend; resolves BACKEND_URL
  src/
    app/
      page.tsx                     Main chat interface (streaming ReAct output, tool thoughts)
      settings/[tab]/page.tsx      Dynamic settings tab routing
      api/                         Server-side proxy routes (chat, agents, orchestrations,
                                   schedules, logs, models — forward to Python backend)
    components/
      SettingsView.tsx             Settings modal shell
      VaultMention.tsx             @-mention vault files in chat prompts
      CollectDataForm.tsx          Renders collect_data tool forms inline in chat
      orchestration/               Visual workflow editor
        WorkflowCanvas.tsx         ReactFlow drag-and-drop DAG canvas
        StepNode.tsx               Step node rendering
        StepConfigPanel.tsx        Step configuration sidebar
        StateSchemaEditor.tsx      Workflow shared-state schema editor
      settings/                    Settings tab components —
                                   AgentsTab, McpServersTab, ModelsTab, OrchestrationTab,
                                   CustomToolsTab, MessagingTab, SchedulesTab, VaultTab,
                                   ReposTab, DBsTab, LogsTab, UsageTab, MemoryTab,
                                   ImportExportTab, IntegrationsTab, GeneralTab, …
        import-export/             ImportView, ExportView, ExamplesView
    store/
      settingsSlice.ts             Redux Toolkit — agents, MCP servers, custom tools, models
    types/                         Shared TypeScript type definitions

backend/
  main.py                          Entry point — loads .env, starts uvicorn on port 8765
  core/
    server.py                      FastAPI app — route registration, startup/shutdown lifecycle
    react_engine.py                ReAct agent loop — LLM calls, tool parsing, iteration
    tools.py                       Tool aggregation (MCP + built-in + custom); system prompt builder
    llm_providers.py               Multi-provider LLM callers (OpenAI, Anthropic, Gemini,
                                   xAI, DeepSeek, Ollama) with retry + backoff
    session.py                     Conversation history + ephemeral session state (store-backed)
    memory.py                      ChromaDB vector store for semantic cross-session memory
    scheduler.py                   Async task scheduler — cron + interval, persists next_run_at
    mcp_client.py                  MCP session manager (stdio + remote Streamable HTTP/SSE,
                                   OAuth 2.0 PKCE + Bearer token, auto-refresh)
    vault.py                       Auto-saves large tool outputs to the blob store; resolves @[path]
    models.py                      Pydantic models: ChatRequest, ChatResponse, Agent, DBConfig
    config.py                      Timeouts, shipped settings defaults, settings-provider hook
    tenancy.py                     The tenant a call is executing for (a ContextVar)
    store/                         The database — one SQLAlchemy layer over SQLite and Postgres
      models.py                    Tables; every tenant-scoped one carries tenant_id
      engine.py                    Engine/session factory; SYNAPSE_DB_URL or a local SQLite file
      migrate.py                   Idempotent additive schema changes (no Alembic in OSS)
      importer.py                  One-time import of a pre-database install's JSON
      resources.py                 Orchestrations, agents, custom tools, MCP servers
      settings.py / schedules.py / sessions.py / usage.py / collections.py
    storage/                       Tenant content as blobs — vault, run logs, cached responses
    runtime_dirs.py                The few things that genuinely need a real directory
    agent_logger.py                Per-run debug logs for agent calls
    schedule_logger.py             Per-run logs for scheduled executions
    model_pricing.py               Shipped rate card; seeds the model_pricing table
    usage_tracker.py               Token + cost tracking over the usage_logs table
    profiling.py                   TimingMiddleware + optional pyinstrument / tracemalloc
    routes/                        REST API endpoints —
                                   chat, agents, tools, orchestrations, sessions, schedules,
                                   messaging, settings, auth, repos, db_configs, vault,
                                   logs, usage, import_export, n8n, profiling, …
    orchestration/
      engine.py                    DAG runner — walks step graph, checkpoints, yields SSE events
      steps.py                     Step executors: Agent, LLM, Tool, Evaluator, Parallel,
                                   Merge, Loop, Transform, Human, End
      state.py                     Shared state + checkpointing across steps
      context.py                   Execution context builder; trace + memory injection
      summarizer.py                Smart truncation + LLM-assisted context compression
      logger.py                    Per-run structured audit log, written as a tenant blob
    messaging/                     Multi-channel messaging subsystem
      manager.py                   Channel lifecycle — start/stop adapters, route inbound messages
      store.py                     Channel config persistence (a store collection)
      adapters/                    Platform adapters: Slack, Discord, Teams, Telegram, WhatsApp
  tools/                           Built-in tool implementations (run as stdio MCP servers)
    sandbox.py                     Docker-sandboxed Python code execution + shared file vault
    sql_agent.py                   SQL query executor (PostgreSQL, MySQL, SQLite)
    web_scraper.py                 Crawl4ai-powered web scraper with stealth mode
    code_search.py                 Semantic code search via vector embeddings
    pdf_parser.py                  PDF text and table extraction
    xlsx_parser.py                 Excel file parsing
    time.py                        Natural language date/time parsing
    collect_data.py                Dynamic form generation (rendered inline in frontend)
    personal_details.py            Personal data read/write tools
  services/                        Business logic services
    code_indexer.py                CocoIndex repo indexing + vector DB operations
    google.py                      Google API integrations (Drive, Calendar, Gmail)
    synthetic_data.py              Synthetic dataset generation
  var/                             A running install's data — gitignored, never committed
    synapse.db                     The database, when it is the default SQLite one
    blobs/<tenant>/                Vault files, run and agent logs, cached responses
    state/<tenant>/                ChromaDB's index — rebuildable, per replica
```

**Frontend ↔ Backend:** The Next.js dev server proxies `/api/*` and `/auth/*` to `http://127.0.0.1:8765` via `next.config.ts` rewrites. Server-side API routes use the `BACKEND_URL` environment variable (default `http://127.0.0.1:8765`).

**MCP Transport:** Local servers use stdio. Remote servers use Streamable HTTP (SSE) with OAuth 2.0 PKCE or Bearer token auth. Synapse manages token refresh and session lifecycle automatically.

**Where state goes.** There is no data directory, and no module may invent one. Everything an install persists belongs to exactly one of three layers, and which one it is decides where the code goes:

| Kind of thing | Layer | Example |
|---|---|---|
| A document with an identity | `core/store/` — a database row | an orchestration, an agent, a schedule, a setting, an OAuth token |
| Tenant content, too file-shaped to be a row | `core/storage/` — a blob key | a vaulted tool output, a run log, a cached response |
| A genuine directory on disk | `core/runtime_dirs.py` | ChromaDB's index, a browser profile |

Two rules follow, and both were learned the hard way:

- **Never derive a path from `__file__`.** Several modules did, which meant the configured location never reached them and a refactor moved the real directory out from underneath them silently. `backend/tests/unit/test_vault_paths.py` exists because of exactly that.
- **Never cache a path at import time.** Paths carry the tenant, and one process serves many. A module-level constant pins every tenant to whoever imported first.

Scratch — genuinely throwaway working files — is `core/storage/scratch.py`. If you find yourself wanting a fourth category, that is a design conversation, not a new directory.

## Adding a Built-in MCP Tool

1. Create `backend/tools/my_tool.py` — implement a standard MCP server using the `mcp` library
2. Register it in `backend/core/server.py` in the `AGENTS` dict:
   ```python
   AGENTS = {
       ...
       "my_tool": str(TOOLS_DIR / "my_tool.py"),
   }
   ```
3. The tool's functions are automatically registered and available to the agent

## Adding an API Route

1. Create `backend/core/routes/my_route.py` with a FastAPI `APIRouter`
2. Register it in `backend/core/server.py`:
   ```python
   from core.routes.my_route import router as my_router
   app.include_router(my_router)
   ```

## PR Checklist

- [ ] No secrets or API keys committed (`backend/var/` is a running install's data and is never committed)
- [ ] New state goes through `core/store/`, `core/storage/` or `core/runtime_dirs.py` — no path from `__file__`, no path cached at import
- [ ] `next.config.ts` still has `output: 'standalone'`
- [ ] New env vars documented in `.env.example`
- [ ] Frontend server-side routes use `process.env.BACKEND_URL`

## Publishing a Release

```bash
# 1. Bump version in pyproject.toml and package.json

# 2. Build and publish Python package
bash scripts/build_frontend.sh
pip install hatch && hatch build
twine upload dist/*

# 3. Build and publish npm package (the launcher and the installer; npm run build makes the installer)
npm publish --access public

# Or: push a version tag and let GitHub Actions handle it
git tag v0.2.0 && git push --tags
```
