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
    session.py                     Conversation history + ephemeral session state (JSON-backed)
    memory.py                      ChromaDB vector store for semantic cross-session memory
    scheduler.py                   Async task scheduler — cron + interval, persists next_run_at
    mcp_client.py                  MCP session manager (stdio + remote Streamable HTTP/SSE,
                                   OAuth 2.0 PKCE + Bearer token, auto-refresh)
    vault.py                       Auto-saves large tool outputs to disk; resolves @[path] mentions
    models.py                      Pydantic models: ChatRequest, ChatResponse, Agent, DBConfig
    config.py                      Settings loader — resolves SYNAPSE_DATA_DIR, credentials
    json_store.py                  Thread-safe JSON file persistence with optional TTL cache
    agent_logger.py                Per-run debug logs for agent calls → logs/agent_logs/
    schedule_logger.py             Per-run logs for scheduled executions → logs/schedule_logs/
    usage_tracker.py               Token + cost tracking; priced via data/model_pricing.json
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
      logger.py                    Per-run structured audit log → logs/orchestration_logs/
    messaging/                     Multi-channel messaging subsystem
      manager.py                   Channel lifecycle — start/stop adapters, route inbound messages
      store.py                     Channel config persistence (messaging_channels.json)
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
  data/                            User data — gitignored
    user_agents.json               Agent configurations
    orchestrations.json            Orchestration definitions
    mcp_servers.json               Remote MCP server configs
    custom_tools.json              Custom tool definitions
    settings.json                  App settings (model, keys, preferences)
    schedules.json                 Schedule definitions
    messaging_channels.json        Messaging channel configurations
    db_configs.json                Database connection configurations
    repos.json                     Indexed repository records
    usage_logs.json / model_pricing.json   Token usage log + per-model pricing table
    chat_sessions/                 Per-session conversation history (JSON)
    chroma_db/                     ChromaDB persistent vector store
    vault/                         Agent file storage
  logs/                            Execution logs — gitignored
    agent_logs/                    Per-agent-run debug traces
    orchestration_logs/            Per-orchestration-run structured audit logs
    orchestration_runs/            Checkpointed run state (JSON, used for resume)
    schedule_logs/                 Per-schedule-run execution logs
```

**Frontend ↔ Backend:** The Next.js dev server proxies `/api/*` and `/auth/*` to `http://127.0.0.1:8765` via `next.config.ts` rewrites. Server-side API routes use the `BACKEND_URL` environment variable (default `http://127.0.0.1:8765`).

**MCP Transport:** Local servers use stdio. Remote servers use Streamable HTTP (SSE) with OAuth 2.0 PKCE or Bearer token auth. Synapse manages token refresh and session lifecycle automatically.

**Data directory:** All user data is stored in `SYNAPSE_DATA_DIR` (default `backend/data/` in dev, `~/.synapse/data/` in packaged installs). Never hardcode paths relative to `__file__` — always read from `core.config.DATA_DIR`.

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

- [ ] No secrets or API keys committed (check `backend/data/` files)
- [ ] Data paths use `DATA_DIR` from `core.config`, not hardcoded paths
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

# 3. Build and publish npm package
node scripts/bundle-frontend.js
npm publish --access public

# Or: push a version tag and let GitHub Actions handle it
git tag v0.2.0 && git push --tags
```
