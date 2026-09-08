"""
Logging for the engine's runtime paths.

The engine used to narrate itself with `print(..., flush=True)` — several
hundred of them across the LLM, ReAct, orchestration, MCP and worker modules,
one per step, per checkpoint, per SSE event on a worker. Every line went to
stdout at the same weight, on every deployment, with no way to turn any of it
down. This module is the one place that decides what is emitted.

    from core.log import get_logger
    logger = get_logger(__name__)
    logger.debug("ENGINE: ▶ step=%s", step_id)

`configure()` is called once by each process entry point (the API server and
the worker). It sets the level of the `core` logger tree from
SYNAPSE_LOG_LEVEL (default INFO) and installs a stdout handler only when the
host has not already configured logging — uvicorn and ARQ each own their own
loggers, and neither wants a second root handler. Records still propagate to
the root, so a host's handlers and pytest's caplog see them.

Levels, as used by the migrated call sites:
  debug    the narration — step boundaries, checkpoints, tool results,
           MCP session chatter. Off by default.
  info     something an operator wants once per run or per process.
  warning  a fallback taken, a retry, a best-effort write that failed.
  error    a step or a provider call that failed for good.
"""
from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: The logger tree this configures. Everything under backend/core lives here.
ROOT_NAME = "core"

_configured = False


def configure(level: str | None = None) -> None:
    """Set the engine's log level; install a stdout handler if nothing has.

    Idempotent. `level` overrides SYNAPSE_LOG_LEVEL; the default is INFO,
    which silences the narration and keeps the warnings.
    """
    global _configured
    name = (level or os.getenv("SYNAPSE_LOG_LEVEL") or "INFO").strip().upper()
    resolved = logging.getLevelName(name)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    if not _configured:
        if not logging.getLogger().handlers:
            # Root stays at WARNING so third-party libraries are not suddenly
            # verbose; the `core` tree carries its own level below.
            logging.basicConfig(level=logging.WARNING, format=_FORMAT, stream=sys.stdout)
        _configured = True

    logging.getLogger(ROOT_NAME).setLevel(resolved)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
