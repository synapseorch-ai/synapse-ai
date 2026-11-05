<p align="center">
  <a href="https://synapseorch.com">
    <img src="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/banner.png" alt="SynapseOrch — AI automation you can read, approve and rely on" width="100%" />
  </a>
</p>

<p align="center">
  <a href="https://synapseorch.com"><img src="https://img.shields.io/badge/Website-synapseorch.com-0f71b0" alt="Website"></a>
  <a href="https://docs.synapseorch.com"><img src="https://img.shields.io/badge/Docs-docs.synapseorch.com-0f71b0" alt="Docs"></a>
  <a href="https://discord.gg/9UN45qyGh8"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/synapseorch-ai/synapse-ai"><img src="https://img.shields.io/github/stars/synapseorch-ai/synapse-ai?style=social" alt="GitHub stars"></a>
  <br />
  <a href="https://pypi.org/project/synapse-orch-ai/"><img src="https://img.shields.io/pypi/v/synapse-orch-ai?logo=pypi&logoColor=white&label=pypi&color=0f71b0" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/synapse-orch-ai"><img src="https://img.shields.io/npm/v/synapse-orch-ai?logo=npm&label=npm&color=0f71b0" alt="npm"></a>
  <a href="https://hub.docker.com/r/synapseorchai/synapse-ai"><img src="https://img.shields.io/docker/pulls/synapseorchai/synapse-ai?logo=docker&logoColor=white&label=docker&color=0f71b0" alt="Docker pulls"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-0f71b0" alt="License: AGPL-3.0"></a>
</p>

**Hand AI the work your team repeats. Approve what goes out.**

Describe a job in plain words. SynapseOrch turns it into steps you can read, runs them exactly as you approved, every
time, and asks before anything goes out. Any model, your own tools and documents, on your laptop or your servers.
Free and open source under AGPL-3.0, with no account and no licence check.

<p align="center">
  <a href="#install"><strong>Install</strong></a> ·
  <a href="https://docs.synapseorch.com"><strong>Documentation</strong></a> ·
  <a href="https://discord.gg/9UN45qyGh8"><strong>Discord</strong></a> ·
  <a href="https://synapseorch.com/cloud"><strong>SynapseOrch Cloud</strong></a>
</p>

<!-- DEMO VIDEO: paste the user-attachments link GitHub gives for demo.mp4 on its own line here, so it plays inline. -->

<p align="center">
  <img src="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/demo.gif" alt="One sentence to Synapse AI: it asks where the notes live, shows a plan with its cost, and builds the workflow; then the workflow runs, waits for you to approve the summary and the save, and finishes with what each step cost" width="100%" />
</p>

---

## Describe it. Approve it. Rely on it.

- **Describe it.** Tell **Synapse AI** the job. It writes the steps, picks a model for each and marks where you sign
  off, then shows you the plan before anything is built.
- **Approve it.** Read every step. When a run reaches yours, it waits for your yes — in the app, or in Slack,
  Telegram or another chat channel.
- **Rely on it.** It runs on schedule exactly as you approved, and shows what each step did and what it cost. A run
  never passes the spending limit you set.

<table>
<tr>
<td width="38%" valign="top"><img src="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/plan-card.png" alt="Synapse AI's plan: the steps, the schedule, what a run costs, and what it asks you first" width="100%" /><br /><sub><b>Describe it.</b> Synapse AI shows the plan, what a run costs and what it will ask you — before it builds anything.</sub></td>
<td width="62%" valign="top"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/home-dark.png" />
  <img src="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/home-light.png" alt="Home: one run waiting on you, with this week's feedback summary to approve" width="100%" />
</picture><br /><sub><b>Approve it.</b> When a run reaches your step, it waits on Home for your yes.</sub></td>
</tr>
</table>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/result-dark.png" />
  <img src="https://raw.githubusercontent.com/synapseorch-ai/synapse-ai/master/.github/assets/readme/result-light.png" alt="A finished run: completed in 37 s, 12 steps, the time it waited on you, and what each step cost" width="100%" />
</picture>
<p align="center"><sub><b>Rely on it.</b> Every run shows the path it took, how long it waited on you, and what each step cost.</sub></p>

## What's inside

| | |
|---|---|
| **Synapse AI** | An assistant that builds and changes agents and workflows from a sentence, and answers questions about your workspace. |
| **Workflows** | Steps you can read on a canvas: agents, models, tools, code, branches, loops, parallel paths, sign-offs and other workflows. Runs follow the path you drew. |
| **Agents and teams** | Agents with their own model, tools, skills and memory. A team agent splits work among its members; any agent can start short-lived helpers. |
| **Sign-offs** | A step that waits for a person — answered in the app or in a chat channel. Approvals for risky tool calls, too. |
| **Knowledge** | Your files and web pages, indexed; answers cite the passage they used. *(Optional part — see below.)* |
| **Boards** | A shared to-do board for people and agents, with each card's history. *(Off until you turn it on in Settings.)* |
| **Apps and tools** | MCP servers (local or remote, OAuth included), REST and OpenAPI, Python functions, SQL databases, web fetch and scraping. |
| **Channels** | Your agents on Slack, Discord, Telegram, Microsoft Teams, WhatsApp, a website chat widget, or a phone number (Twilio). |
| **Any model** | Your own keys for 23+ providers, any OpenAI-compatible endpoint, local models through Ollama, or the Claude Code, Codex, Gemini and Copilot CLIs you already use. A different model at every step. |
| **Costs you can read** | An estimate before a run, what each step cost after it, and a hard limit per run. |
| **Use it from anywhere** | A command line, a versioned REST API, an MCP server for Claude, ChatGPT and Cursor, and an OpenAI-compatible endpoint where every agent answers as a model. |
| **Scale mode** | Redis queue, a worker fleet, Postgres checkpoints and S3 storage, for when one process is not enough. |

---

## Install

**macOS / Linux**
```bash
curl -LsSf https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.sh | bash
```

**Windows (PowerShell)**
```powershell
irm https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.ps1 | iex
```

Nothing needs to be installed first: the installer brings its own Python and, if yours is missing or too old,
Node.js — no sudo. It asks which AI to use (and whether to start on login) while it installs, and ends with the link.
Your data and settings live in `~/.synapse`. Add `--yes` (`curl … | bash -s -- --yes`) to take every default.

<details>
<summary><strong>npm, pip or Docker</strong></summary>

```bash
npm install -g synapse-orch-ai && synapse      # the first run uses the same installer
pip install synapse-orch-ai && synapse         # Python 3.11–3.14 and Node.js 20.9+
```

```bash
docker run -d --name synapse -p 3000:3000 -p 127.0.0.1:8765:8765 \
  -v synapse-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  synapseorchai/synapse-ai:latest
```

Then open `http://localhost:3000`. The backend's port (`8765`) is published on this machine only: it serves an
internal API, so never publish it on `0.0.0.0`.
</details>

**Optional parts.** Knowledge brings Docling and a CPU build of PyTorch, so it is added only when you ask:

```bash
synapse upgrade --with knowledge                              # an existing install
curl -LsSf …/setup.sh | bash -s -- --with knowledge           # a new one
```

Then turn it on in Settings → General. Every later upgrade keeps it.

## Upgrading

```bash
synapse upgrade
```

It backs up the database, installs the newest version, updates the database and starts Synapse again if it was running.
An npm install is upgraded with `npm install -g synapse-orch-ai@latest`; Docker by pulling the new image and running
the same command.

**Coming from 1.x?** Run the install command above, or `synapse upgrade`. Your agents, workflows, chats, schedules,
settings, API keys and vault files move into 2.0's store. Before anything changes, the upgrade backs up your 1.x data
folder, logs and `.env` to `~/.synapse/backups/synapse-1.x-<date>.tar.gz`, with a `manifest.json` that lists where each
part came from; the old install's folder is removed once 2.0 is running. To go back, reinstall 1.9.3 and put each part
of the backup back where `manifest.json` says. Don't run 2.0's `synapse uninstall` first: it removes `~/.synapse`,
backups included, unless you add `--keep-data`. Docker imports a 1.x `/data` volume on its first start. See
[Upgrading to 2.0](https://docs.synapseorch.com/getting-started/upgrading-to-2).

---

## The command line

One `synapse` command runs SynapseOrch on this machine, and drives any SynapseOrch — this one, one on your servers, or
Cloud — with an API key. Everything you can do in the app, you can do from a terminal or a script.

**This machine**
```bash
synapse start                        # open it (-d: in the background)
synapse status                       # is it running? (--json for scripts)
synapse logs -f                      # follow the backend's log (frontend, install)
synapse upgrade                      # the newest version; data and settings stay
synapse autostart on                 # start when you log in
```

**Build, run and answer**
```bash
synapse agents list                  # list, show, create, update, delete
synapse workflows list               # list, show, export, create, validate, deploy, delete
synapse run NAME -m "…"              # run a workflow and follow it to its answer
synapse runs waiting                 # what waits on you: questions and held tool calls
synapse runs answer RUN_ID --approve # answer a sign-off (or --reject, or reply with text)
synapse chat --agent NAME -m "…"     # one message to an agent (bare `synapse chat` to talk)
synapse schedules create --workflow NAME --cron "0 9 * * MON"
```

**Knowledge, Boards and the workspace**
```bash
synapse knowledge upload --base Handbook ./policies/*.pdf --wait
synapse knowledge search "refund window" --base Handbook
synapse boards show BOARD            # a board's open cards; `boards card` adds and comments
synapse activity --days 7            # what ran, what failed, what waits
synapse usage --by model             # what the workspace spent, by day, agent or model
```

**Every door a key has**
```bash
synapse tools list                   # every product tool this key may call; `tools call NAME`
synapse api GET /agents              # call any route a key reaches, as `gh api` does
synapse mcp                          # serve SynapseOrch to Claude, Cursor… over stdio
synapse login --url https://api.synapseorch.com --api-key-stdin   # drive another SynapseOrch
```

For scripts, `--json` prints JSON (one event a line for a run) and `-q` lists ids only. Exit codes say what happened:
`0` done · `3` waiting on a person · `4` run failed · `5` not found · `6` key refused · `7` busy or unreachable.
`synapse --help` lists every command. [CLI reference →](https://docs.synapseorch.com/cli/commands)

## Use it from your own code and tools

- **REST API** — start runs, stream their events, answer sign-offs, with an API key.
  [API →](https://docs.synapseorch.com/api/overview)
- **MCP server** — build and run workflows from Claude, ChatGPT or Cursor.
  [MCP server →](https://docs.synapseorch.com/api/mcp-server)
- **OpenAI-compatible endpoint** — every agent answers as a model to any OpenAI client, with its own tools.

## Scale mode

When one process is not enough: a Redis job queue, 1 to 100+ workers, per-step Postgres checkpoints (a worker that dies
loses nothing), S3-compatible storage and per-team run limits — behind the same API.

```bash
docker pull synapseorchai/synapse-ai-api-server:latest   # stateless API server
docker pull synapseorchai/synapse-ai-worker:latest       # workers, as many as needed
```

`docker compose --profile scale up` runs the whole stack (with `REDIS_URL` set in `.env`); Kubernetes examples are in
`infra/k8s/`.
[Scale mode →](https://docs.synapseorch.com/scale/overview)

## Running it on a network

Everything an agent can do runs on the machine Synapse runs on, so treat a shared instance like a server:

- turn on **login** (Settings → General) on anything reachable beyond your own machine;
- publish only the web port (`3000`) on the network, never the backend's (`8765`);
- local MCP servers and custom Python tools run commands: turn them off with `allow_stdio_mcp` if you do not trust
  every user, and give agents the narrowest tools that do the job.

Security issues: please report them privately — see [SECURITY.md](SECURITY.md).

---

## SynapseOrch Cloud

The same product, run for you: no servers, managed models, plans with a hard monthly limit, and teams with roles.
[synapseorch.com](https://synapseorch.com)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, how the pieces fit, and the PR checklist.
Questions and ideas are welcome on [Discord](https://discord.gg/9UN45qyGh8).

## License

SynapseOrch is licensed under the [GNU AGPL v3](LICENSE).

[![Star History Chart](https://api.star-history.com/svg?repos=synapseorch-ai/synapse-ai&type=date&legend=top-left)](https://www.star-history.com/#synapseorch-ai/synapse-ai&type=date&legend=top-left)
