# CLI — synapse

## Prerequisites

| Dependency | Minimum | Required |
|------------|---------|----------|
| Python | 3.11 | Yes |
| Node.js | 20.9.0 | Yes |
| npm | bundled with Node | Yes |
| ollama | any | No (local models only) |

`synapse start`, `restart`, `setup` and `upgrade` warn on stderr if versions are below the minimums. Node is needed only to run Synapse; the commands that drive it (below) need Python alone. Missing `ollama` is a non-fatal warning — cloud API models (Anthropic, OpenAI, Gemini) still work.

## Installation

```bash
# editable install (recommended for development)
python -m pip install -e .

# or install normally
python -m pip install .
```

Run the interactive setup wizard before first start to configure API keys and settings:

```bash
python setup.py
# or after install:
synapse setup
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNAPSE_DB_URL` | `sqlite+aiosqlite:///backend/var/synapse.db` | Database holding settings, agents, orchestrations, tools, schedules, runs and usage |
| `SYNAPSE_BLOB_DIR` | `backend/var/blobs` | Vault, run logs and cached responses (or set the `S3_*` variables) |
| `SYNAPSE_STATE_DIR` | `backend/var/state` | Rebuildable per-replica state (the ChromaDB index) |
| `SYNAPSE_BACKEND_PORT` | `8765` | Backend API server port |
| `SYNAPSE_FRONTEND_PORT` | `3000` | Frontend web UI port |
| `SYNAPSE_PROFILING` | `false` | Enable performance profiling (set by `--profile`) |
| `SYNAPSE_API_URL` | this machine's backend | The Synapse the API commands talk to: an origin such as `https://api.synapseorch.com` or `http://localhost:3000` (same as `--url`) |
| `SYNAPSE_API_KEY` | — | The API key for `SYNAPSE_API_URL` (same as `--api-key`). Not needed for this machine's own Synapse |
| `SYNAPSE_CLI_CONFIG` | `~/.synapse/cli.json` | Where the CLI keeps its own key and login session |

Values in a `.env` file at the project root are loaded automatically (variables already in the environment are not overridden).

## Commands

```bash
# start in foreground (opens browser)
synapse start

# start in background (writes pidfiles)
synapse start --detach

# start but don't open browser
synapse start --no-browser

# start on custom ports
synapse start --backend-port 8080 --frontend-port 4000

# start with performance profiling enabled
synapse start --profile

# stop processes (reads pidfiles)
synapse stop

# show status
synapse status

# restart
synapse restart

# restart detached on custom ports
synapse restart --detach --backend-port 8080 --frontend-port 4000

# run interactive setup wizard (configure API keys and settings)
synapse setup

# create or update the database schema, and import a pre-database install
synapse migrate

# pull latest code and rebuild everything
synapse upgrade

# uninstall Synapse AI (removes all files, executable, and PATH entries)
synapse uninstall

# uninstall but keep the database, vault and logs
synapse uninstall --keep-data
```

If you prefer running without installing:

```bash
python -m synapse start
```

## Command reference

### `start`

Starts the backend and frontend. Waits for both to be ready, then opens the browser (unless `--no-browser` or `--detach`).

In foreground mode, `Ctrl+C` shuts down both processes cleanly. In detach mode, PIDs are written to the data directory and the process returns immediately.

| Flag | Default | Description |
|------|---------|-------------|
| `--detach`, `-d` | off | Run in background and write pidfiles |
| `--no-browser` | off | Do not open a browser on start |
| `--backend-port PORT` | `8765` | Port for the backend API server (overrides `SYNAPSE_BACKEND_PORT`) |
| `--frontend-port PORT` | `3000` | Port for the frontend web UI (overrides `SYNAPSE_FRONTEND_PORT`) |
| `--profile` | off | Enable performance profiling (`SYNAPSE_PROFILING=true`) |

### `stop`

Reads PID files and terminates both processes. Sends `SIGTERM` first, then `SIGKILL` if the process does not exit within 5 seconds. On Windows uses `taskkill /F /T` to kill the full process tree.

### `status`

Prints whether the backend and frontend processes are running or have a stale PID.

### `restart`

Equivalent to `stop` followed by `start`. Accepts the same port and detach flags as `start` (but not `--no-browser` or `--profile`).

| Flag | Default | Description |
|------|---------|-------------|
| `--detach`, `-d` | off | Leave processes detached after restart |
| `--backend-port PORT` | `8765` | Port for the backend API server |
| `--frontend-port PORT` | `3000` | Port for the frontend web UI |

### `setup`

Runs the interactive setup wizard. Prompts for API keys and settings and writes them to the database, creating it if it does not exist.

### `migrate`

Creates the database if it is missing, applies any additive schema changes, and imports a pre-database install's JSON files if one is found. Prints what it did.

The server does the same work on its first boot after an upgrade, so this is never required. It exists for the cases where starting the server is the wrong way to ask: a deployment that wants the schema in place before the first request, a container entrypoint or Kubernetes init container, and anyone who would rather see what a migration did than infer it from the logs.

```bash
synapse migrate
```

### `upgrade`

Pulls the latest code and rebuilds everything in place:

1. Stops running services
2. `git pull --ff-only` in the project root
3. Recreates the backend virtual environment (`backend/venv`) and reinstalls requirements
4. Reinstalls the `synapse-ai` package in editable mode
5. Runs `npm install` and `npm run build` in the frontend directory

```bash
synapse upgrade
```

### `uninstall`

Permanently removes Synapse AI. Prompts for confirmation before proceeding.

Steps performed:
1. Stops running services
2. Removes startup registration (systemd user service on Linux, LaunchAgent on macOS, Registry run key on Windows)
3. Removes the user's data (`backend/var` — the database, vault, blob store and logs) and Synapse home (`~/.synapse`) — both skipped with `--keep-data`
4. Removes the installation directory (project root), including `backend/venv` and `frontend/node_modules`
5. Runs `pip uninstall -y synapse-ai` to remove the package and console script; falls back to deleting the `synapse` executable directly if found on `PATH`; on Windows also removes `synapse.exe` / `synapse-script.py` from the Python Scripts directory
6. Cleans `PATH` additions from `~/.bashrc`, `~/.zshrc`, `~/.bash_profile`, `~/.profile` (Unix) or the user `Environment` registry key (Windows)

| Flag | Default | Description |
|------|---------|-------------|
| `--keep-data` | off | Preserve `backend/var` and `~/.synapse` when uninstalling |

## Driving Synapse from the terminal

These commands talk to a running Synapse over its `/api/v1` API, so the same
commands drive this machine's Synapse, one on another machine, or Synapse Cloud.

```bash
synapse agents list                       # and: agents show <id|name>
synapse orch list                         # and: orch show <id|name>  (orch = orchestrations)

synapse run "Weekly report" -m "this week" -i pages=3
synapse run "Weekly report" --detach      # print the run id and leave it running

synapse runs list --status paused
synapse runs show <run_id>
synapse runs logs <run_id> -f
synapse runs answer <run_id> "yes"        # or --field name=value …, or --approve / --reject
synapse runs cancel <run_id>

synapse schedules create --orchestration "Weekly report" --cron "0 9 * * 1" --prompt "this week" --paused
synapse schedules list                    # and: show, pause, resume, trigger, delete

synapse chat                              # a conversation; /help lists its commands
synapse chat --agent Research -m "summarise today's news"
echo "summarise this" | synapse chat

synapse mcp                               # serve this Synapse to an MCP client over stdio
```

**Names or ids.** Agents and orchestrations can be named in any case, or given by id.

**This machine's Synapse needs no setup.** The first command mints an API key for
the CLI, named "Synapse CLI" (you will see it in Settings → API keys), and keeps it
in `~/.synapse/cli.json`. Revoking it there rotates it: the next command mints
another. If you have turned on the app's login, the CLI asks for it too, once
every 7 days, as the browser does:

```bash
synapse login
synapse logout
```

**Another Synapse needs its URL and an API key:**

```bash
export SYNAPSE_API_URL=https://api.synapseorch.com
export SYNAPSE_API_KEY=sk-syn-…
synapse runs list
# or per command: synapse runs list --url … --api-key …
```

**A run that asks a person.** At a terminal, `run` asks each question as it comes
and carries on. In a script, it prints the run id and exits with `3`. Answer with
`synapse runs answer`. In a chat, a tool call the agent needs permission for is
asked about in the chat.

**For scripts.** Every command takes `--json`. `list` and `show` print the API's
JSON as it is. `run`, `runs logs -f` and `chat` print one event per line. Nothing
else is written to stdout; notes and progress go to stderr, so `synapse run x >
out.txt` keeps only the answer.

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | An error: the server refused, could not be reached, or the name matched nothing |
| 2 | The command line was wrong, or it needs a person (`schedules delete` without `--yes`) |
| 3 | A run is waiting for an answer |
| 4 | A run failed or was cancelled |

## PID files

PID files are process bookkeeping, not user data, and are written per user:

- `~/.synapse/run/backend.pid`
- `~/.synapse/run/frontend.pid`

## Profiling

The `profile` subcommand queries and controls backend performance profiling. Requires the backend to be running with `--profile`.

```bash
# show per-endpoint latency table (avg, p50, p95, p99, max)
synapse profile stats

# clear collected timing stats
synapse profile reset

# start CPU profiling
synapse profile cpu-start

# print CPU profile report (text or HTML)
synapse profile cpu-report
synapse profile cpu-report --output report.html

# start memory profiling
synapse profile memory-start

# snapshot current memory allocations (top 20 by default)
synapse profile memory-snapshot
synapse profile memory-snapshot --limit 50

# record a py-spy flame graph (requires: pip install py-spy)
synapse profile spy
synapse profile spy --output profile.svg --duration 60
```

### `profile` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--output FILE`, `-o` | — | Output file (`cpu-report`: `.html`, `spy`: `.svg`) |
| `--limit N` | `20` | Top allocations to show (`memory-snapshot`) |
| `--duration SECS` | `30` | Recording duration in seconds (`spy`) |

## Example quick flow

```bash
# install and run in background
python -m pip install -e .
synapse setup
synapse start --detach
synapse status
# when finished
synapse stop
```

## Extensibility

The CLI is extensible — future commands (migrations, backup, logs) can be added to `synapse.cli`.
