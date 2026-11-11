"""Where the backend listens (F1534).

On this computer only, by default. Every caller of the backend is here: the frontend (its BACKEND_URL is
http://127.0.0.1:<port>), the CLI's readiness probes, the MCP bridge, and a tunnel to localhost for a phone number's
webhooks. Listening on 0.0.0.0, as `synapse start` did on every OS, let a device on the same Wi-Fi reach the API's docs,
Google's sign-in callback and the metrics with no token. The frontend still listens on the network: it is how a phone
on the same Wi-Fi opens the app, with its link.

`SYNAPSE_BACKEND_HOST` (in the environment or the install's .env) is the one way to listen wider, and what was chosen
is said when the backend starts. The Docker images set it to 0.0.0.0: inside a container that is where its published
port (127.0.0.1:8765 in docker-compose.yml) and the compose network reach it.
"""
from __future__ import annotations

import os

ENV = "SYNAPSE_BACKEND_HOST"
THIS_COMPUTER = "127.0.0.1"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def backend_host() -> str:
    """The address the backend listens on: `SYNAPSE_BACKEND_HOST`, or this computer only."""
    return (os.getenv(ENV) or "").strip() or THIS_COMPUTER


def listening_words(host: str, port: int) -> str:
    """What the backend says it listens on, and how to change it."""
    if host in _LOOPBACK:
        return (f"listening on {host}:{port}, this computer only "
                f"(set {ENV}=0.0.0.0 to reach the backend itself from other devices)")
    return f"listening on {host}:{port}, which other devices can reach ({ENV})"
