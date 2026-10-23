# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through
[GitHub Security Advisories](https://github.com/synapseorch-ai/synapse-ai/security/advisories/new),
which creates a private thread visible only to the maintainers.

If you cannot use GitHub advisories, email **info@synapseorch.com** with `SECURITY` in the subject.

Please include:

- what the issue is and why it is a problem
- the version or commit you tested (`synapse --version`)
- how the instance was deployed — local, Docker, or scale mode
- steps to reproduce, and a proof of concept if you have one

### What to expect

| | |
|---|---|
| First response | within 5 working days |
| Assessment and severity | within 10 working days |
| Fix and coordinated disclosure | agreed with you, normally within 90 days |

We will credit you in the advisory unless you would rather stay anonymous. We do not run a paid
bounty programme.

## Supported versions

Security fixes land on the latest minor release. Older versions are not backported — please upgrade.

## Deployment guidance

Synapse runs AI agents that execute tools on your machine. Most reported issues come from exposing
an instance that was configured for local use, so a few defaults are worth knowing.

**Do not publish the backend port on a public interface.** The backend (default `8765`) hosts the
internal API and the agent chat endpoints. Reach it through the frontend on port `3000` instead.
Docker Compose binds the backend to `127.0.0.1` by default; `SYNAPSE_BACKEND_BIND` overrides this,
and changing it exposes those routes to your network.

**Set `SYNAPSE_INTERNAL_TOKEN`.** It gates the internal `/api/*` and `/chat` routes. Docker images
generate one automatically on first boot. Without it, those routes are restricted to loopback
callers, so a network-exposed instance without a token will reject remote requests rather than
serve them.

**Enable login** (Settings → General) on anything reachable beyond your own machine.

**Treat these as remote code execution by design**, and only enable them where you trust every user
who can reach the instance:

- **stdio MCP servers** — registering one runs a local command. Disable with `allow_stdio_mcp`, or
  restrict with `mcp_command_allowlist`. Force-disabled in scale mode.
- **Custom Python tools** and the **`transform` step** — run customer-authored Python. The Docker
  runtime sandboxes them. A transform runs with **no network** (since 2.0): it reaches outside through
  a tool step. A transform's code from an imported bundle is listed in the import's preview and
  warnings.
- **`SYNAPSE_TRANSFORM_RUNTIME=host`** — set only in the server's environment, never through settings
  (`POST /api/settings` refuses it). It runs transforms outside the sandbox, **as the server, able to
  read everything the server can, including stored keys, with the network**; their environment is cut
  to a short allowlist (no tokens, keys, secrets or URLs). Only for a single-user machine you trust
  every workflow on.
- **The `bash` tool** and **`execute_python`** — run commands on the host or in a container.

**Agents are subject to prompt injection.** Any content an agent reads — a web page, a document, a
tool result — can attempt to steer it. Give agents the narrowest tool set that does the job, and
prefer human-approval steps for anything irreversible.

**API keys** (`sk-syn-…`) carry full access to the v1 and v2 APIs. Treat them as passwords, and
rotate them from Settings → API Keys if one leaks.

## Past advisories

Published advisories are listed under
[Security → Advisories](https://github.com/synapseorch-ai/synapse-ai/security/advisories).
