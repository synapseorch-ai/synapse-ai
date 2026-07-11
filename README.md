# Synapse AI — Multi-Agent Orchestration Platform

<p align="center">
  <img src="https://github.com/user-attachments/assets/c673ea6f-4979-4b38-93ae-c594ac3d641c" alt="synapse-ai-github" width="600" />
</p>

<p align="center">
  <a href="https://synapseorch.com"><img src="https://img.shields.io/badge/Website-synapseorch.com-0A0A0A?logo=vercel&logoColor=white" alt="Website"></a>
  <a href="https://docs.synapseorch.com"><img src="https://img.shields.io/badge/Docs-docs.synapseorch.com-blue?logo=readthedocs&logoColor=white" alt="Docs"></a>
  <a href="https://discord.gg/9UN45qyGh8"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/synapseorch-ai/synapse-ai"><img src="https://img.shields.io/github/stars/synapseorch-ai/synapse-ai?style=social" alt="GitHub stars"></a>
  <a href="https://github.com/synapseorch-ai/synapse-ai?tab=AGPL-3.0-1-ov-file"><img src="https://img.shields.io/github/license/synapseorch-ai/synapse-ai" alt="License"></a>
  <a href="https://www.npmjs.com/package/synapse-orch-ai"><img src="https://img.shields.io/npm/v/synapse-orch-ai?logo=npm&label=npm" alt="npm"></a>
  <a href="https://pypi.org/project/synapse-orch-ai/"><img src="https://img.shields.io/pypi/v/synapse--orch-ai?logo=pypi&logoColor=white&label=pypi" alt="PyPI"></a>
  <a href="https://hub.docker.com/r/synapseorchai/synapse-ai"><img src="https://img.shields.io/docker/pulls/synapseorchai/synapse-ai?logo=docker&logoColor=white&label=docker" alt="Docker Pulls"></a>
</p>

*Build AI workflows that actually ship.*

**Wire agents, tools, and LLMs into deterministic pipelines — without the framework lock-in.** Synapse is an open-source platform for creating, connecting, and orchestrating AI agents powered by any LLM — local or cloud. Agents use real tools: browsing the web, querying databases, executing code, reading files, managing emails, and anything else you can expose through an MCP server, a webhook, or a Python script.

<p align="center">
  <a href="https://synapseorch.com"><strong>🌐 Website</strong></a> · 
  <a href="https://docs.synapseorch.com"><strong>📖 Documentation</strong></a> · 
  <a href="https://discord.gg/9UN45qyGh8"><strong>💬 Discord</strong></a>
</p>

---

## Install

### Quick Setup Script (recommended)

**macOS / Linux:**
```bash
curl -sSL https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/main/setup.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/main/setup.ps1 | iex
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
```bash
docker run -d \
  -p 3000:3000 \
  -v synapse-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  synapseorchai/synapse-ai:latest
```

Then open `http://localhost:3000`. See the [Docker guide](https://docs.synapseorch.com/getting-started/installation#docker) in the docs for custom ports and environment variable configuration.

> **Security:** publish only the frontend port `3000` — the backend (`8765`) serves an internal API and should not be exposed on `0.0.0.0` (docker-compose binds it to `127.0.0.1`). The container auto-generates a shared internal token so that surface is authenticated by default. For any network-reachable deployment, enable **login** (Settings → Security) and/or set `allow_stdio_mcp=false`, since registering a stdio MCP server launches local commands.

### Upgrading

| Install method | Upgrade command |
|---|---|
| Bash / PowerShell installer (recommended) | `synapse upgrade` |
| pip | `pip install --upgrade synapse-orch-ai` |
| npm | `npm update -g synapse-orch-ai` |
| Docker | `docker pull synapseorchai/synapse-ai:latest` |

---

## Scale Mode

Run unlimited agents and orchestrations concurrently. When you need to go beyond a single process, the distributed scale layer handles the load:

- **Redis Cluster** — job queue, SSE event streams, pub/sub cancellation signals, auto-failover
- **ARQ worker fleet** — 1 to 100+ independent workers, each running up to 20 concurrent orchestrations; autoscale with KEDA on queue depth
- **PgBouncer** — multiplexes hundreds of worker connections into a small, stable Postgres pool
- **S3 artifact storage** — stream large file outputs directly to AWS S3, Cloudflare R2, or MinIO
- **Multi-tenant quotas** — per-team or per-customer concurrent run limits with HTTP 429 enforcement
- **Per-step checkpoint recovery** — worker crashes don't lose jobs; the next worker resumes from the last completed step

Three Docker images — pull only what you need:

```bash
docker pull synapseorchai/synapse-ai:latest             # full app (standalone mode)
docker pull synapseorchai/synapse-ai-api-server:latest  # stateless API server (scale mode)
docker pull synapseorchai/synapse-ai-worker:latest      # worker process (scale mode, run as many as needed)
```

The `docker-compose.yml` in the repo spins up the full stack. Production K8s manifests are in `infra/k8s/`.

📖 [**Scale Mode docs →**](https://docs.synapseorch.com/scale/overview)

---

## What Makes Synapse Different

- **Multi-Model Orchestrations** — Run a different LLM at every step. Use a fast model for routing, a powerful one for analysis. You control where the compute goes.
- **Deterministic DAG Execution** — Orchestrations follow the exact path you designed. No hallucinated detours.
- **Turn Anything Into a Tool** — Python scripts, REST APIs, webhooks, MCP servers, or entire orchestrations — all become agent-callable tools.
- **Human-in-the-Loop** — Pause workflows for human review. Resumable across restarts. Connect via UI, Slack, Telegram, or any messaging channel.
- **Scales to Millions of Requests** — The distributed scale layer separates API servers, Redis job queues, and independent worker processes so you can run any number of agents or orchestrations concurrently. Start on one machine, grow to a Kubernetes cluster — the V2 API never changes.
- **Local-First, No Lock-In** — Full local operation with Ollama. Mix local and cloud models freely. Your data stays yours.
- **Built-In Scheduling & Messaging** — Cron-based automation with results pushed to Slack, Discord, Telegram, Teams, or WhatsApp.
- **14+ LLM Providers** — Cloud, local, and CLI providers including Ollama, OpenAI, Anthropic, Gemini, xAI, DeepSeek, AWS Bedrock, and more.

📖 [**Learn more →**](https://docs.synapseorch.com)

---

## Synapse UI

https://github.com/user-attachments/assets/7a5ab42c-5fae-4f13-876c-13aa9b5a0366

## Demos

### Content Writing Orchestration
Multi-agent pipeline that researches a topic, drafts content in a Google Doc, and returns the shared link. *(Video is 2x speed)*

https://github.com/user-attachments/assets/4eec5db8-70d0-47b6-8608-f52b1f7b7d68

### Autonomous Code Development & PR Creation
Multi-agent system with human-in-the-loop that writes code and generates pull requests autonomously.

https://github.com/user-attachments/assets/95a511e1-e3e9-4812-b9ca-f7f4c28ef80f

### Native Orchestration Builder
Chat with the AI builder — describe what you want, and it creates the orchestration DAG for you.

https://github.com/user-attachments/assets/282cc99d-cdea-4ad0-b648-f22112c6e295

---

## Key Concepts

| Concept | Summary |
|---|---|
| **Agents** | Independent ReAct loops with their own system prompt, tools, model, and repos. [Docs →](https://docs.synapseorch.com/agents/overview) |
| **Orchestrations** | DAGs of steps — wire agents together with routing, parallelism, loops, and human gates. [Docs →](https://docs.synapseorch.com/orchestrations/overview) |
| **Tool Ecosystem** | 10+ native tool servers, built-in MCP servers, remote MCP via OAuth/PAT, and custom HTTP/Python tools. [Docs →](https://docs.synapseorch.com/tools/overview) |
| **AI Builder** | A meta-agent that designs and materializes orchestrations from natural language. [Docs →](https://docs.synapseorch.com/orchestrations/ai-builder) |
| **Schedules** | Cron/interval automation with messaging notifications. [Docs →](https://docs.synapseorch.com/integrations/scheduling) |
| **Messaging** | Slack, Discord, Telegram, Teams, WhatsApp — with multi-agent mode. [Docs →](https://docs.synapseorch.com/integrations/messaging) |
| **Scale Mode** | Distributed execution layer: Redis job queue, independent worker fleet, per-step Postgres checkpoints, S3 artifact storage, and multi-tenant quotas. [Docs →](https://docs.synapseorch.com/scale/overview) |
| **V2 API** | Stable versioned REST API for building products on top of Synapse — enqueue, stream, cancel, webhooks. [Docs →](https://docs.synapseorch.com/api/overview) |
| **Vault** | Persistent file storage shared across agents and sessions. [Docs →](https://docs.synapseorch.com/vault) |

---

## CLI

```bash
synapse start     # start backend + frontend, open browser
synapse stop      # stop background processes
synapse upgrade   # upgrade to the latest version
synapse uninstall # remove Synapse, wipe ~/.synapse, and uninstall the package
```

---

## Roadmap

- **Spawn Sub-Agent Tool** — Agents natively spawn and delegate tasks to temporary sub-agents mid-execution.
- **Compact Conversations** — Automatic message history compression for large contexts.
- **Global Variables** — Dynamic variables injectable into prompts, orchestrations, tools, and MCP environments.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=synapseorch-ai/synapse-ai&type=date&legend=top-left)](https://www.star-history.com/#synapseorch-ai/synapse-ai&type=date&legend=top-left)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, architecture details, how to add MCP tool servers, and the PR checklist.

## License

Synapse AI is licensed under AGPL v3 — see [LICENSE](LICENSE)
