"""Third-party libraries' usage tracking, off before any of them loads (F1567).

Synapse sends nothing home, and says so (the site, the README, the FAQ). Some of the libraries it ships phone home
unless told not to: a fresh 2.0.8 connected to `cocoindex.gateway.scarf.sh` at every backend start, because
cocoindex's engine sends usage tracking when the package is imported (SHN1, 11-13). Each such library reads a switch
from the environment, so `core/__init__.py` sets them all the moment anything of the backend is imported — the
backend (`main.py`), the worker, every tool server and the Knowledge parser, in a wheel or a Docker image — and every
process it starts inherits them. The CLI (`synapse/cli.py`) reads this file for itself, for what it starts.

A switch already set is kept: someone who wants a library's tracking on can still say so.

Not here, because they send nothing on their own: Synapse's own OpenTelemetry (`core/telemetry`, off until an
operator points it at their own collector), FastAPI's (off in `core.telemetry.fastapi_options`), and the SDK headers
a model provider's client puts on the calls a person asked for.
"""
from __future__ import annotations

import os
from typing import MutableMapping

SWITCHES: dict[str, str] = {
    # cocoindex's Rust engine: a Scarf request when the package is imported.
    "COCOINDEX_DISABLE_USAGE_TRACKING": "true",
    # Chroma's product telemetry (PostHog), for every client: the locked release's is a no-op, but `chromadb>=0.5`
    # allows the ones that sent it. `core/memory.py` also turns it off for its own client.
    "ANONYMIZED_TELEMETRY": "False",
    # The Hugging Face Hub's, which transformers reads too (Knowledge's Docling models).
    "HF_HUB_DISABLE_TELEMETRY": "1",
    # The shared switch: the Hub reads it, and so may the tools Synapse starts.
    "DO_NOT_TRACK": "1",
    # Next.js's, for the web where Next runs from a checkout (`frontend/next.config.ts` and `scripts/load-env.js` set
    # it as well, for a build or a dev server started by hand).
    "NEXT_TELEMETRY_DISABLED": "1",
}


def turn_off(environ: MutableMapping[str, str] | None = None) -> None:
    """Set each switch this environment leaves unset or empty."""
    environ = os.environ if environ is None else environ
    for name, value in SWITCHES.items():
        if not (environ.get(name) or "").strip():
            environ[name] = value
