# Synapse AI — Multi-Agent Orchestration Platform

<p align="center">
  <img src="https://github.com/user-attachments/assets/c673ea6f-4979-4b38-93ae-c594ac3d641c" alt="synapse-ai-github" width="600" />
</p>

<p align="center">
  <a href="https://discord.gg/9UN45qyGh8"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/naveenraj-17/synapse-ai"><img src="https://img.shields.io/github/stars/naveenraj-17/synapse-ai?style=social" alt="GitHub stars"></a>
  <a href="https://github.com/naveenraj-17/synapse-ai?tab=AGPL-3.0-1-ov-file"><img src="https://img.shields.io/github/license/naveenraj-17/synapse-ai" alt="License"></a>
  <a href="https://www.npmjs.com/package/synapse-orch-ai"><img src="https://img.shields.io/npm/v/synapse-orch-ai?logo=npm&label=npm" alt="npm"></a>
  <a href="https://pypi.org/project/synapse-orch-ai/"><img src="https://img.shields.io/pypi/v/synapse--orch-ai?logo=pypi&logoColor=white&label=pypi" alt="PyPI"></a>
  <a href="https://hub.docker.com/r/synapseorchai/synapse-ai"><img src="https://img.shields.io/docker/pulls/synapseorchai/synapse-ai?logo=docker&logoColor=white&label=docker" alt="Docker Pulls"></a>
</p>

*Build AI workflows that actually ship.*

**Wire agents, tools, and LLMs into deterministic pipelines — without the framework lock-in.** Synapse is an open-source platform for creating, connecting, and orchestrating AI agents powered by any LLM — local or cloud. Agents use real tools: browsing the web, querying databases, executing code, reading files, managing emails, trading stocks, and anything else you can expose through an MCP server, a webhook, or a Python script — if you can write it, agents can use it.

Businesses use Synapse to convert their existing APIs and Python programs into agent tools, orchestrate them into end-to-end workflows, and build AI-powered products on top of the REST API — without starting from scratch or locking into a vendor.

---

## Prerequisites

- **Python 3.11+**
- **Node.js 22+**
- **uvx** — `pip install uv` (used to run MCP servers)

> Don't have these? The setup script will attempt to install any missing prerequisites automatically.

---

## Install

### Quick Setup Script (recommended)
The easiest way to get started. Clones the repository, installs all dependencies, and starts both servers automatically.

**macOS / Linux:**
```bash
curl -sSL https://raw.githubusercontent.com/naveenraj-17/synapse-ai/main/setup.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/naveenraj-17/synapse-ai/main/setup.ps1 | iex
```

### npm
```bash
npm install -g synapse-orch-ai
synapse
```

### pip
```bash
pip install synapse-orch-ai
synapse
```

### Docker
No Python or Node.js required on the host. Ideal for teams deploying on shared infrastructure or servers.

```bash
docker run -d \
  -p 3000:3000 \
  -v synapse-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  synapseorchai/synapse-ai:latest
```

Then open `http://localhost:3000`. Pass your LLM API keys and any config as environment variables:

```bash
docker run -d \
  -p 3000:3000 \
  -v synapse-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e OPENAI_API_KEY=sk-... \
  -e OLLAMA_BASE_URL=http://host-gateway:11434 \
  --add-host=host-gateway:host-gateway \
  synapseorchai/synapse-ai:latest
```

#### Custom ports

Override the default ports (frontend `3000`, backend `8765`) with environment variables:

```bash
docker run -d \
  -p 8080:8080 \
  -p 9000:9000 \
  -e SYNAPSE_FRONTEND_PORT=8080 \
  -e SYNAPSE_BACKEND_PORT=9000 \
  -v synapse-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  synapseorchai/synapse-ai:latest
```

The `-p HOST:CONTAINER` values must match the `-e` values.

### Upgrading

| Install method | Upgrade command |
|---|---|
| Bash / PowerShell installer (recommended) | `synapse upgrade` |
| pip | `pip install --upgrade synapse-orch-ai` |
| npm | `npm update -g synapse-orch-ai` |
| Docker | `docker pull synapseorchai/synapse-ai:latest` |

---

## CLI

Once installed, use the `synapse` command to manage the server:

```bash
synapse start     # start backend + frontend, open browser
synapse stop      # stop background processes
synapse upgrade   # upgrade to the latest version
synapse uninstall # remove Synapse, wipe ~/.synapse, and uninstall the package
```


---

## What Makes Synapse Different

Most agent frameworks hand you a loop, a few toy tools, and a tutorial. Synapse is a production-grade orchestration platform built for the real thing:

### Cut Costs Without Cutting Quality
Run a different model at every step. Use a fast, cheap model for routing and classification; switch to a powerful model only for the steps that need it. One orchestration, many models — you control exactly where the compute goes.

### Workflows That Actually Do What You Designed
Orchestrations are strict DAGs. Execution follows the exact path you defined — no surprises, no hallucinated detours. For steps where the next action is already known (fetch this, parse that, write here), use **Tool** and **LLM** steps instead of full agents: zero reasoning overhead, deterministic output, and far cheaper to run.

### Turn Anything Into a Tool
Your existing systems are already the capability — Synapse just makes them available to agents:
- **Any Python program** → drop it in, it becomes a sandboxed agent tool
- **Any REST API or webhook** → describe its schema, agents call it natively
- **Any MCP server** → local subprocess or remote HTTP, connected in seconds
- **Any orchestration** → promote it to an agent; chain orchestrations like functions

This is the path most businesses take: existing CRM APIs, internal Python scripts, ML models, and third-party services all become agent-callable tools without a rewrite.

### Never Blocked on a Human Decision
**Human** steps pause execution mid-workflow and wait. When the person responds — via the UI, Slack, Telegram, or any connected messaging channel — the run resumes exactly where it left off. No polling, no timeouts you didn't set.

### Run It Anywhere, Own Your Data
Full local operation with Ollama. Or mix: local models for some agents, cloud APIs for others. No vendor lock-in on models, no data sent anywhere you didn't choose. Persistent vault stores files across agent sessions on your machine.

### Built-In Scheduling & Messaging
Schedule any agent or orchestration to run on a cron or interval. Results are pushed directly to Slack, Discord, Telegram, Teams, or WhatsApp — with multi-agent mode so users can switch agents mid-chat.

---

## Under the Hood

| Capability | Detail |
|---|---|
| **Multi-model orchestrations** | Per-step model override — mix Gemini Flash, Claude Opus, and local Ollama in one workflow |
| **Orchestrations as agents** | Promote any orchestration to an agent; nest pipelines inside pipelines |
| **Deterministic Tool steps** | Skip the ReAct loop entirely — call a specific tool directly with state values |
| **Resumable human gates** | Human steps survive server restarts; runs pick up exactly where they paused |
| **Docker-sandboxed Python** | Agents write and execute Python in an isolated container — safe by default |
| **Stealth web scraping** | Built-in anti-bot evasion; works on LinkedIn, financial sites, JS-heavy pages |
| **Semantic code search** | Index any repo; agents query it by natural language and get file + line results |
| **Cost limits per run** | Set a max-spend per orchestration run — execution halts if the budget is hit |
| **5 messaging platforms** | Slack, Discord, Telegram, Teams, WhatsApp — with per-channel agent binding |
| **14+ LLM providers** | Cloud, local, and CLI providers; no API key needed for Claude/Gemini/Codex CLI |
| **Import/Export packs** | Portable bundles of agents + orchestrations + MCP configs; 3 curated starter packs |
| **AI Builder** | Chat with a meta-agent that designs and materializes orchestrations for you |

---

## Synapse UI

https://github.com/user-attachments/assets/7a5ab42c-5fae-4f13-876c-13aa9b5a0366

## Synapse Orchestration Demo

### Content Writing Orchestration
This demo showcases a multi-agent content writing orchestration pipeline. The agents autonomously open a browser, research a user-provided topic, draft the content in a Google Doc, and return the shared link. By default, the worker agents utilize `gemini-3-flash-preview`, while the evaluator agents use `gemini-3.1-pro-preview`. Each agent can be configured with different models based on your specific requirements. (Note: The video is sped up 2x to fit on GitHub.)

https://github.com/user-attachments/assets/4eec5db8-70d0-47b6-8608-f52b1f7b7d68

### Autonomous Code Development & PR Creation
This demo highlights a multi-agent software development system that writes code and generates pull requests on its own. A human-in-the-loop step is integrated into each stage, allowing you to review and confirm the agents' actions before the system finalizes the PR and outputs the repository link.

https://github.com/user-attachments/assets/95a511e1-e3e9-4812-b9ca-f7f4c28ef80f

### Native Orchestration Builder
Instead of manually dragging and dropping to create your flow, you can now just chat with the builder. Tell it what kind of orchestration you want, and the AI will build the DAG for you. Once it maps it out, you can just start running it immediately.

https://github.com/user-attachments/assets/282cc99d-cdea-4ad0-b648-f22112c6e295

## The Tool Ecosystem

Synapse agents are powerful because of what they can do. Every tool is a separate MCP process — isolated, composable, and safe.

### Native Tool Servers

These run automatically when Synapse starts:

| Tool Server | What It Does |
|---|---|
| **Sandbox** | Execute Python code in an isolated Docker container (512 MB RAM, 1 CPU). Pre-loaded with pandas, numpy, matplotlib, scikit-learn, requests, and more. Read/write files in the persistent vault. |
| **Vault** | Persistent file storage for agents. Create, read, update, patch, and list files across sessions. JSON deep-merge, text find-replace, and directory listing built in. |
| **SQL Agent** | Connect to any database (PostgreSQL, MySQL, SQLite). List tables, introspect schemas, run read queries. Supports any SQLAlchemy-compatible connection string. |
| **Browser** | Full browser automation via Playwright MCP. Navigate pages, click, fill forms, take screenshots, extract content. Powered by Chromium. |
| **PDF Parser** | Extract text and tables from any PDF by URL. Tables converted to Markdown. Page-by-page extraction. |
| **Excel Parser** | Parse `.xlsx` files from URL. Multi-sheet support. Converts all sheets to Markdown tables. |
| **Collect Data** | Generate dynamic forms that pause execution and collect user input. Supports text, number, email, date, phone, and option fields. |
| **Time** | Natural language date/time parsing. Handles relative offsets, weekday targets, timezone conversions, and complex expressions like "next Friday at 3pm EST". |
| **Code Search** | Semantic code search across indexed repositories using vector embeddings. Search by natural language query, get back relevant code snippets with file paths and line numbers. |
| **Web Scraper** | Powerful web scraping powered by crawl4ai. Scrape any URL to clean markdown, extract structured data with CSS schemas, crawl multiple URLs in parallel, capture screenshots, handle infinite-scroll pages, and run multi-step authenticated sessions. Built-in stealth mode bypasses anti-bot protections — works on LinkedIn, financial sites, and JavaScript-heavy pages. |

### Built-in MCP Servers

Enabled automatically when configured:

| Server | What It Does |
|---|---|
| **Filesystem** (`@modelcontextprotocol/server-filesystem`) | Full read/write access to your local code repositories. Configure which paths to expose in Settings → Repos. |
| **Google Workspace** (`workspace-mcp`) | Gmail (read, search, send), Google Drive (list, read, create files), and Google Calendar (events, scheduling). One-click OAuth setup in Settings. |
| **Playwright** (`@playwright/mcp`) | Browser control — already included in the native Browser tool above, available separately for headless automation. |
| **Sequential Thinking** (`npx @modelcontextprotocol/server-sequential-thinking`) | Structured step-by-step reasoning for complex, multi-stage problems. Agents break tasks into explicit thought chains before acting. Enabled by default. |
| **Memory** (`npx @modelcontextprotocol/server-memory`) | Persistent knowledge graph memory across sessions. Agents store and retrieve facts, relationships, and context between runs. Enabled by default. |

### Remote MCP Servers

Connect to any MCP server over the network — no code needed. Synapse supports native OAuth and Personal Access Token (PAT) authentication.

**To add a remote server:**

1. Open **Settings → MCP Servers**
2. Click the **Remote (URL)** tab at the top of the form
3. Optionally select a **preset** (Vercel, GitHub Copilot, Jira, Zapier, Figma, Fetch) to auto-fill the URL and token fields
4. Enter a **Server Name** and the **Server URL**
5. **Bearer Token / PAT** — leave empty to use OAuth (a browser window will open for authorization), or paste a personal access token for PAT-based servers (GitHub, Figma)
6. Click **Connect Server**

Synapse prefixes external tools with `<server-unique-name>__` followed by the tool name to prevent naming collisions. Any MCP-compatible API becomes an agent tool instantly.

Find more on the [MCP servers registry](https://github.com/modelcontextprotocol/servers).

### Local (stdio) MCP Servers

For servers that run as local processes, click the **Local (stdio)** tab and enter the command and arguments:

```
Command:   uvx
Arguments: mcp-server-git
```

Use the **Git** preset to auto-fill this. Add environment variables (API keys, secrets) directly in the form — no config file editing required.

### Custom Tools — Your APIs and Python Scripts

Turn any existing API or Python program into an agent tool — no rewrites, no new infrastructure.

**Register an API endpoint:**
1. Go to **Settings → Custom Tools** and add a tool with its name, description, endpoint, and parameter schema
2. Agents see the name and description to decide when to call it, and pass parameters automatically
3. Works with any REST API, internal service, or webhook — your CRM, billing system, ML inference endpoint, anything

**Register a Python script:**
1. Paste your existing Python function — it runs in Synapse's sandboxed Docker executor
2. Define its input parameters and expected output shape
3. It becomes a callable tool for any agent you assign it to

This is the fastest path for businesses: your existing Python scripts (ETL jobs, ML models, data processors) and internal APIs become your agents' extended toolkit. Build an n8n workflow, expose it as a webhook, and add it here — 400+ node integrations become one agent tool in minutes.

---

## Building Agents

Create specialized agents in **Settings → Agents**. Each agent is an independent ReAct loop with its own:

- **System prompt** — define its persona, expertise, and constraints
- **Tool selection** — give it access to all tools, or restrict to a specific subset
- **Model override** — run different agents on different models (e.g., fast model for routing, capable model for analysis)
- **Code repositories** — link repos for semantic code search and filesystem access
- **LLM provider** — mix local Ollama models with cloud APIs per agent

### Example: Research Agent

```json
{
  "name": "Research Agent",
  "description": "Deep research using web browsing and document parsing",
  "tools": ["browser_navigate", "browser_snapshot", "parse_pdf", "parse_xlsx", "vault_write"],
  "system_prompt": "You are a thorough research analyst. For any research task: browse primary sources, extract key data, parse any documents you find, and save a structured report to the vault."
}
```

### Example: Data Agent

```json
{
  "name": "Data Agent",
  "description": "Analyzes data files and databases, produces reports",
  "tools": ["list_tables", "get_table_schema", "run_sql_query", "execute_python", "vault_write", "vault_read"],
  "system_prompt": "You are a data analyst. Explore the database schema, write SQL queries to extract insights, then use Python (pandas/matplotlib) to analyze and visualize results. Save all outputs to the vault."
}
```

### Example: Developer Agent

```json
{
  "name": "Strict Developer",
  "description": "Writes production-ready code, creates APIs, and runs self-correcting tests",
  "tools": ["execute_python", "mcp_github", "mcp_slack", "vault_write", "vault_read"],
  "system_prompt": "You are a senior backend engineer. Write robust, functional code, execute it using the Python tool to verify logic, and save the final output to the vault."
}
```

---

## AI Builder

The **Synapse AI Builder** is a native multi-agent orchestration that helps you design, review, and create new orchestrations through a guided conversation — no canvas required.

Describe what you want to build in plain language. The builder will:

1. **Understand** your requirements (and ask clarifying questions if needed)
2. **Draft a plan** — structured markdown with a step-by-step breakdown and an ASCII flow diagram
3. **Present the plan** for your review — approve it or request revisions in plain text
4. **Create any new sub-agents** the orchestration needs (if enabled)
5. **Materialise the orchestration** by calling `create_orchestration` or `update_orchestration`
6. **Confirm** with a friendly summary and the new orchestration ID

The Builder can also **edit existing orchestrations** — point it at an orchestration you are viewing and describe your changes.

The builder is seeded automatically at startup and is always available in the orchestration picker.

---

## Orchestrating Agents

Individual agents are powerful. Orchestrations are transformative.

An orchestration is a directed graph (DAG) of steps — you wire agents together, add routing logic, run things in parallel, loop over datasets, and checkpoint for human review. Build them visually on the canvas or define them in JSON.

### Step Types

| Step | What It Does |
|---|---|
| **Agent** | Run an agent's full ReAct loop. Pass context from shared state as input. Capture the result as an output key. |
| **LLM** | Make a direct LLM call without spinning up a full agent loop. Use for single-shot generation, summarization, classification, or prompt templating against shared state. Faster and cheaper than a full agent step when tool use isn't needed. |
| **Tool** | Execute a specific MCP tool directly — no agent reasoning, no loop. Pass inputs from shared state, write the raw tool output back to state. Ideal for deterministic data-fetching steps (e.g., run a SQL query, read a vault file, call an API). |
| **Evaluator** | Ask an LLM to make a routing decision. Maps decision labels to next steps. Use this to branch based on analysis results. |
| **Parallel** | Run multiple agent branches. Each branch runs sequentially (respects shared resources like browser). |
| **Merge** | Combine outputs from parallel branches. Strategies: list (accumulate), concat (join text), dict (merge objects). |
| **Loop** | Repeat a set of steps N times. Use with transforms to iterate over lists or refine outputs. |
| **Transform** | Execute arbitrary Python against the shared state dict. Reshape data, compute values, filter lists. |
| **Human** | Pause and ask a human for input via a generated form. Execution resumes when the user responds. Fully resumable. |
| **Extract JSON** | Parse JSON out of any text — handles raw JSON, markdown code fences, and multiple objects (stored as an array). No LLM call. Perfect for pulling structured data out of an agent's raw output. |
| **Print** | Render a text or Markdown template with `{state.key}` interpolation and store it in the shared state. Use for building formatted summaries, reports, or notification bodies without an LLM call. |
| **IF / Else** | Evaluate a Python expression against the shared state and branch to one of two steps — true path or false path. Supports dot-notation (`state.result.flag`). Missing keys evaluate to `None`. No LLM call. |
| **Switch** | Match a Python expression's string result against a set of named cases. Each case routes to a different step; unmatched values fall through to the default route. No LLM call. |
| **End** | Finalize the workflow. |

### Deterministic Control-Flow Steps

Four step types execute **without any LLM call** — they are fast, free, and completely predictable. Use them to add control flow and data handling between your agent steps.

#### Extract JSON
Finds and parses JSON from raw text. Works with:
- Plain JSON objects / arrays
- Markdown code fences (` ```json ... ``` `)
- Multiple JSON blocks in a single string (stored as an array)

```
Input key:  llm_raw_output   (e.g. "The answer is: ```json\n{\"score\": 8}\n```")
Output key: parsed           (→ { "score": 8 })
```

#### Print
Renders a Markdown or plain-text template with `{state.key}` and `{state.key.nested}` placeholders resolved from the shared state, then stores the result.

```
print_content: "# Report\n\nScore: {state.score}\nCategory: {state.category}"
output_key:    report_text
```

#### IF / Else
Evaluates a Python expression against the shared state and branches to one of two steps. Dot-notation is supported — missing keys are `None`.

```
if_condition:    state.score > 7
if_true_step_id:  step_approve
if_false_step_id: step_reject
```

Safe built-ins only (`len`, `str`, `int`, `float`, `bool`, `list`, `dict`, `max`, `min`, `abs`, `round`, `any`, `all`). No imports.

#### Switch
Converts a Python expression to a string and matches it against named cases. Unmatched values fall through to `switch_default_step_id`.

```
switch_expression:      state.category
switch_cases:
  "sports"   → step_sports_handler
  "politics" → step_politics_handler
  "science"  → step_science_handler
switch_default_step_id: step_general_handler
```

> **Tip:** Chain these steps to build lightweight classification pipelines — use an LLM step to classify, an **Extract JSON** step to parse its output, and a **Switch** step to route — all without extra LLM calls.

### Shared State

Every step reads from and writes to a shared state dictionary. Define the schema upfront:

```json
"state_schema": {
  "query": { "type": "string", "description": "Initial user query" },
  "research_results": { "type": "string", "description": "Raw research output" },
  "analysis": { "type": "string", "description": "Structured analysis" },
  "approved": { "type": "boolean", "default": false }
}
```

Steps use `input_keys` to pull from state and `output_key` to write back. This is how agents hand off work to each other.

---

## Example: End-to-End Research → Report Orchestration

Here's a complete orchestration that combines 5 agents to go from a question to a published report with human approval:

```
User Query
    │
    ▼
[1. Research Agent]          → Browses web, parses PDFs, saves raw findings to vault
    │ output: research_raw
    ▼
[2. Parallel Step]
    ├── [3. Data Agent]      → Pulls supporting data from SQL, runs Python analysis
    └── [4. Fact Checker]    → Cross-references key claims via browser
    │ output: data_analysis, verified_facts
    ▼
[5. Merge]                   → Combines data_analysis + verified_facts
    │
    ▼
[6. Writer Agent]            → Synthesizes all inputs into structured report, saves to vault
    │ output: report_draft
    ▼
[7. Quality Evaluator]       → Routes: "approved" → Human Review | "needs_revision" → Writer Agent
    │
    ▼
[8. Human Review]            → Shows draft, collects approval or revision notes
    │
    ▼
[9. Publisher Agent]         → Sends report via email (Gmail MCP), posts to Drive
    │
    ▼
[END]
```

This orchestration:
- Runs 3 agents in parallel (saves time)
- Routes automatically based on quality assessment
- Loops the writer if revisions are needed
- Pauses for human approval before publishing
- Uses vault to pass files between agents
- Publishes via Gmail and Google Drive

Build this visually on the canvas in about 10 minutes.

---

## Example: Stock Analysis Orchestration

The included "Stock Intraday Trading" orchestration shows how to combine market data, risk analysis, and human decisions:

```
[1. Portfolio Analyzer]     → Checks current positions via Zerodha MCP
    │
    ▼
[2. Login Router]           → Evaluator: logged in? → continue | not logged in? → prompt user
    │
    ▼
[3. Parallel Analysis]
    ├── [NSE Stock Analyzer]        → Technical analysis on watchlist
    ├── [Beta Data Fetcher]         → Fetches beta/volatility data
    └── [Current Events Agent]      → Browses news, checks sentiment
    │
    ▼
[4. Merge + Strategy Transform]    → Python transform: compute risk-adjusted scores
    │
    ▼
[5. Human Approval]                → Shows recommended trades, waits for confirmation
    │
    ▼
[END]
```

---

## Example: Business Workflow — API-Driven Orchestration

Businesses with existing APIs and Python scripts can wire them directly into orchestrations. Here is a customer renewal pipeline where every step calls your own systems:

```
Customer ID (triggered from your CRM or product event)
    │
    ▼
[1. Customer Agent]           → Calls your CRM API, usage metrics API, support ticket API
    │ output: customer_profile
    ▼
[2. Parallel Analysis]
    ├── [Risk Analyst]        → Runs your churn prediction Python model as a tool
    └── [Finance Agent]       → Calls your billing API for contract value and payment history
    │ output: churn_score, contract_data
    ▼
[3. Merge + Transform]        → Python transform: compute combined risk score
    │
    ▼
[4. Evaluator]                → Routes: "high_risk" → Human Review | "healthy" → Auto-Renew
    │
    ▼
[5. Human Review]             → Account exec reviews summary, approves outreach or escalation
    │
    ▼
[6. Action Agent]             → Updates CRM via API, sends personalized email, posts to Slack
    │
    ▼
[END]
```

Every step in this pipeline calls **your APIs** and runs **your Python models**. Synapse handles the reasoning, routing, and coordination. You own the data, the tools, and the workflow.

---

## Build Products on the Synapse REST API

Synapse exposes a full REST API on port `8765`. Product and engineering teams can trigger agents and orchestrations programmatically from any application — internal dashboards, customer-facing features, or backend services — without building AI infrastructure from scratch.

**Run an agent:**
```bash
POST /api/chat/{agent_id}
{ "message": "Analyze Q3 sales data and flag anomalies" }
```

**Trigger an orchestration:**
```bash
POST /api/orchestrations/{orchestration_id}/run
{ "initial_state": { "customer_id": "cust_8812", "period": "Q3-2025" } }
```

**Poll for results:**
```bash
GET /api/sessions/{session_id}/status
```

Your application controls the trigger and consumes the result. Synapse handles the agent reasoning, tool execution, LLM calls, and workflow state in between.

---

## Schedules

Automate agent and orchestration runs on a recurring schedule.

- **Interval** — run every N minutes/hours/days (e.g. monitor a feed, poll an API)
- **Cron / Fixed Time** — run at specific times (e.g. every day at 9 AM for a morning standup report)
- **Prompt** — The prompt is what the agent will receive each time the schedule fires. Think of it as a standing instruction.
- **Messaging notifications** — If the selected agent has a connected messaging channel (Slack, Telegram, etc.), the result is sent there automatically after each run.

---

## Messaging

Connect your agents to Telegram, Discord, Slack, Teams, or WhatsApp.

Enable **Multi-Agent Mode** in a channel so users can switch agents mid-chat using `/agent <name>` and list them with `/agents`. The channel's bound agent is the default.

---

## Import / Export

Export your orchestrations, agents, MCP servers, and tools as a portable bundle, or import one from another Synapse instance.

**Example Packs:** Synapse includes curated collections of agents, orchestrations, and MCP servers. Select a pack to preview what will be imported before committing:
- **Starter Pack** — Get up and running fast, includes a Personal Assistant with full tool access and a Web Research Agent.
- **Developer Pack** — Built for engineering teams, includes a Code Review Agent, Software Engineer Agent, QA Engineer, and a Dev base orchestration.
- **Productivity Pack** — Business and content power-users, includes a Data Analyst, Content Writer, Jira Analyst, Slack Notifier.

---

## Vault Management

The **Vault** is a persistent file directory (`data/vault/`) that acts as shared storage for all your agents. 

- **UI File Explorer** — Manage your vault directly in **Settings → Vault**. Create, edit, and delete `.md`, `.json`, and `.txt` files in a full-featured markdown and JSON editor.
- **Context Injection** — Instantly reference files inside agent system prompts or orchestration templates by typing `@`. The UI provides an intelligent dropdown that searches the vault, allowing you to seamlessly embed documents, guides, or skill configurations into LLM contexts as `@[path/to/file.md]`.
- **Agent Access** — Agents have built-in tool access to dynamically read, write, and patch files in the vault across sessions.

---

## Configuration

### Supported LLM Providers

| Provider | Mode | Model prefix | Notes |
|---|---|---|---|
| **Ollama** | Local | *(none — bare model name)* | Any model pulled via `ollama pull`. Default: `mistral-nemo` |
| **Anthropic** | Cloud | `claude-` | Claude 3.5, Claude 3 Opus, Claude 3.7 Sonnet, etc. |
| **OpenAI** | Cloud | `gpt-` | GPT-4o, GPT-4 Turbo, o1, o3-mini, etc. |
| **Gemini** | Cloud | `gemini-` / `gemma-` | Gemini 1.5 Pro, Gemini 2.0 Flash, etc. |
| **xAI (Grok)** | Cloud | `grok-` | Grok-2, Grok-3, Grok-3 Mini. Set `XAI_API_KEY`. |
| **DeepSeek** | Cloud | `deepseek-` | DeepSeek-V3, DeepSeek-R1 (reasoning). Set `DEEPSEEK_API_KEY`. |
| **AWS Bedrock** | Cloud | `bedrock.` | Any Bedrock model (Converse API). Set AWS credentials or a Bedrock API key in Settings. |
| **Ollama v1 Compatible** | Cloud | `oaic.<model>` | Any cloud OpenAI-compatible endpoint (OpenRouter, Together AI, Fireworks, etc.). Configure Base URL + API key in Settings → Model. |
| **Local v1 Compatible** | Local | `locv1.<model>` | Any local OpenAI-compatible server (vLLM, LM Studio, Jan, Ollama `/v1`, etc.). Configure Base URL (and optional key) in Settings → Model. |
| **Claude CLI** | CLI | `cli.claude` | Requires the [Claude Code](https://claude.ai/code) CLI (`claude`) installed and authenticated. No API key needed — uses your existing Claude subscription. |
| **Gemini CLI** | CLI | `cli.gemini` | Requires the [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`) installed and authenticated. Supports `pro` and `flash` variants. |
| **Codex CLI** | CLI | `cli.codex` | Requires the [Codex CLI](https://github.com/openai/codex) (`codex`) installed and authenticated. No API key needed — uses your existing OpenAI subscription. |
| **GitHub Copilot CLI** | CLI | `cli.copilot` | Requires the [GitHub Copilot CLI extension](https://docs.github.com/en/copilot/github-copilot-in-the-cli) (`copilot`) installed and authenticated. No API key needed — uses your GitHub Copilot subscription. |

Switch providers per-agent or globally in **Settings → Model**.


## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=naveenraj-17/synapse-ai&type=date&legend=top-left)](https://www.star-history.com/#naveenraj-17/synapse-ai&type=date&legend=top-left)

---

### Environment Variables

```bash
# Copy and edit
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `SYNAPSE_DATA_DIR` | `~/.synapse/data` | Where agents store files, memory, and state |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint |
| `SYNAPSE_BACKEND_PORT` | `8765` | Backend API port |
| `SYNAPSE_FRONTEND_PORT` | `3000` | Frontend UI port |
| `BACKEND_URL` | `http://127.0.0.1:8765` | Backend URL as seen by Next.js server (set in Docker) |
| `CORS_ORIGINS` | `http://localhost:3000` | Allowed CORS origins |

---

## Manual Setup

### Backend

```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3.11 main.py
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:<SYNAPSE_FRONTEND_PORT>` (default: `http://localhost:3000`)

---

## Upcoming Features (Roadmap)

We are constantly improving Synapse AI. Here are a few features currently in the pipeline:

- **Spawn Sub-Agent Tool:** Allow agents to natively spawn and delegate tasks to temporary sub-agents mid-execution.
- **Compact Conversations:** A conversation option optimized to handle large contexts smoothly, compressing message history automatically.
- **Global Variable:** Support for defining global variables that can be dynamically injected into agent prompts, orchestrations, custom tools, and MCP server environments.
---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, architecture details, how to add MCP tool servers, and the PR checklist.

## License

Synapse AI is licensed under AGPL v3 to ensure it remains open and free, and to prevent cloud monopolies from offering it as a managed service without contributing back to the community.

AGPL-3.0-only — see [LICENSE](LICENSE)
