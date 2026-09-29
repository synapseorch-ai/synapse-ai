# CLI — synapse

## Prerequisites

None to install first. The setup script (`releases/latest/download/setup.sh`, or
`setup.ps1` on Windows) brings Python through uv and, when the system's is missing
or older than 20.9, Node.js into `~/.synapse/runtime` — no sudo. A pip install
needs Python 3.11–3.14 and Node.js 20.9+ on `PATH`. `ollama` is optional (local
models only).

## Installation

```bash
curl -LsSf https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.sh | bash
```

The installer shows its steps, asks which AI to use and whether to start on login
(while it installs), and ends with the link. `--yes` takes every default. It also
upgrades a 1.x install it finds, after a backup. See `synapse/installer/`.

For development, from a checkout:

```bash
python -m pip install -e .
synapse start
```

A checkout keeps its data in `backend/var` and its `.env` at the root; an
installed Synapse keeps both under `SYNAPSE_HOME` (below).

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNAPSE_HOME` | `~/.synapse` for an installed Synapse; unset in a checkout | Where an installed Synapse keeps everything it writes: `var/` (the defaults below), `.env`, `logs/`, `run/`, `runtime/`, `backups/`, `install.json` |
| `SYNAPSE_DB_URL` | `sqlite+aiosqlite:///<var>/synapse.db` (`<var>` is `$SYNAPSE_HOME/var`, or `backend/var` in a checkout) | Database holding settings, agents, orchestrations, tools, schedules, runs and usage |
| `SYNAPSE_BLOB_DIR` | `<var>/blobs` | Vault, run logs and cached responses (or set the `S3_*` variables) |
| `SYNAPSE_STATE_DIR` | `<var>/state` | Rebuildable per-replica state (the ChromaDB index) |
| `SYNAPSE_BACKEND_PORT` | `8765` | Backend API server port |
| `SYNAPSE_FRONTEND_PORT` | `3000` | Frontend web UI port |
| `SYNAPSE_PROFILING` | `false` | Enable performance profiling (set by `--profile`) |
| `SYNAPSE_API_URL` | this machine's backend | The Synapse the API commands talk to: an origin such as `https://api.synapseorch.com` or `http://localhost:3000` (same as `--url`) |
| `SYNAPSE_API_KEY` | — | The API key for `SYNAPSE_API_URL` (same as `--api-key`). Not needed for this machine's own Synapse |
| `SYNAPSE_CLI_CONFIG` | `~/.synapse/cli.json` | Where the CLI keeps its own key and login session |

Values in `$SYNAPSE_HOME/.env` (a checkout: the project root's `.env`) are loaded automatically; variables already in the environment are not overridden.

## Commands

```bash
# start in foreground (opens browser)
synapse start

# start in background (writes pidfiles)
synapse start --detach

# show the servers' output here instead of in their logs
synapse start --verbose

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

Starts the backend and frontend, waits for both to answer, and prints one line:
`Synapse is running → <link>` — the access link while login is off. It opens the
browser unless `--no-browser` or `--detach`. The servers' output goes to
`<home>/logs/backend.log` and `frontend.log` (rotated at 10 MB, three kept); `synapse
logs` shows it. A server that exits while starting is reported at once, with the end
of its log.

In foreground mode, `Ctrl+C` shuts down both processes cleanly. In detach mode, PIDs
are written to `<home>/run/` and the command returns.

| Flag | Default | Description |
|------|---------|-------------|
| `--detach`, `-d` | off | Run in background and write pidfiles |
| `--no-browser` | off | Do not open a browser on start |
| `--verbose`, `-v` | off | Show the servers' output in this terminal instead of their logs |
| `--backend-port PORT` | `8765` | Port for the backend API server (overrides `SYNAPSE_BACKEND_PORT`) |
| `--frontend-port PORT` | `3000` | Port for the frontend web UI (overrides `SYNAPSE_FRONTEND_PORT`) |
| `--profile` | off | Enable performance profiling (`SYNAPSE_PROFILING=true`) |

### `stop`

Reads PID files and terminates both processes. Sends `SIGTERM` first, then `SIGKILL` if the process does not exit within 5 seconds. On Windows uses `taskkill /F /T` to kill the full process tree.

### `status`

Prints whether the backend and frontend processes are running or have a stale PID.
`--json` prints one object — the PIDs, whether the frontend answers (`ready`), and
its `url` and `link` — for scripts, and for the installer.

### `logs`

```bash
synapse logs              # the backend's last 50 lines
synapse logs frontend -f  # and follow
synapse logs install      # what the installer ran
```

### `restart`

Equivalent to `stop` followed by `start`. Accepts the same port and detach flags as `start` (but not `--no-browser` or `--profile`).

| Flag | Default | Description |
|------|---------|-------------|
| `--detach`, `-d` | off | Leave processes detached after restart |
| `--backend-port PORT` | `8765` | Port for the backend API server |
| `--frontend-port PORT` | `3000` | Port for the frontend web UI |

### `setup`

Runs the interactive setup wizard. Prompts for API keys and settings and writes them to the database, creating it if it does not exist.

`synapse setup --apply -` writes settings from JSON on stdin instead — `{"settings":
{...}, "default_agent": true}` — which is how the installer saves its answers
without an API key appearing in the process list. Only known keys are written, and
only the keys given.

### `autostart`

`synapse autostart on|off|status` — start Synapse when you log in (a systemd user
service, a LaunchAgent, or a Run key; the same question the installer asks).

### `migrate`

Creates the database if it is missing, applies any additive schema changes, and imports a pre-database install's JSON files if one is found. Prints what it did.

The server does the same work on its first boot after an upgrade, so this is never required. It exists for the cases where starting the server is the wrong way to ask: a deployment that wants the schema in place before the first request, a container entrypoint or Kubernetes init container, and anyone who would rather see what a migration did than infer it from the logs.

```bash
synapse migrate
```

`--from DIR` and `--logs DIR` (both repeatable) import named 1.x folders even into
a database that already has content — the first named wins where two hold the same
item — and `--json` prints one report on stdout, exiting 1 if any folder failed.
The installer uses them for a 1.x install whose files are not where the server
looks.

### `upgrade`

Installs the newest release over this one; data and settings stay:

1. Stops Synapse if it is running
2. Backs up the SQLite database to `<home>/backups/`
3. Installs the new version — with that release's own installer for a script
   install, with pip for a pip install
4. Updates the database
5. Starts Synapse again if it was running

`--version X` installs a given version. A checkout is told to `git pull`; an npm
install is told `npm install -g synapse-orch-ai@latest`. On a 1.x script install,
`synapse upgrade` (1.x's own) brings 2.0's code down and hands over to the
installer, which moves the data and removes the old folder.

```bash
synapse upgrade
```

### `uninstall`

Removes Synapse. Prompts for confirmation before proceeding.

1. Stops running services
2. Removes start-on-login (systemd user service, LaunchAgent, or Run key)
3. Removes the program (`uv tool uninstall` for a script install, `pip uninstall`
   for pip; npm is told `npm uninstall -g synapse-orch-ai`)
4. Removes `~/.synapse` — the database, vault, settings, logs and backups — or, with
   `--keep-data`, only the runtime, logs and PID files

| Flag | Default | Description |
|------|---------|-------------|
| `--keep-data` | off | Keep the data, settings and backups in `~/.synapse` for a later reinstall |

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

PID files are process bookkeeping, not user data: `<home>/run/backend.pid` and
`frontend.pid` for an installed Synapse, `backend/var/run/` in a checkout — so
`synapse stop` in a checkout never stops an installed Synapse on the same machine.

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
