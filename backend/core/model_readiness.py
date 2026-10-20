"""
Whether the chat page's model can answer at all, asked before anyone types (F182).

A fresh install's default model is `ollama.mistral`. With nothing listening, the
chat page greeted the person with "Ready" and answered their first message with
an HTTP library's error text; nothing had checked anything before saying Ready.
`/api/status` now answers that question, and the page says what is missing and
where to fix it.

Only what can be proven is reported, and nothing is spent finding out:

* a key provider with no key saved (keys come only from settings; nothing reads
  them from the environment);
* an OpenAI-compatible server, a local one or the model gateway with no address;
* Ollama not reachable, refusing its model list, or without the model pulled —
  one local call to `/api/tags` with a short timeout, since a page load waits on
  it (a refused port fails at once).

* a model CLI that is not on this computer — a PATH lookup, nothing run
  (ONB2, D303): a person whose installer answer was Claude Code and who later
  removed it was told "Ready", then answered with "not found in PATH".

A saved key is not tried: trying it is a paid call. Bedrock (boto's default
credential chain) can authenticate outside settings, and whether a CLI is
signed in is its own command's to say (too slow for a page load), so neither
is called "not ready" on a guess.
"""
from __future__ import annotations

import httpx

from core.llm_providers import _ollama_base_url, detect_provider_from_model
from core.providers import cli_status
from core.providers.cli_invocation import CLI_BINARIES, split_cli_model
from core.providers.registry import PROVIDERS

#: What each provider needs saved before it can answer, from the provider table:
#: {provider id: (setting, label)}.
_KEYED = {p.id: (p.keys[0], p.label) for p in PROVIDERS if p.readiness == "key"}
_ADDRESSED = {p.id: (p.address, p.label) for p in PROVIDERS if p.readiness == "address"}

#: A page load waits on the model list, so it is short.
TAGS_TIMEOUT = 2.0


async def _ollama_models(base: str) -> list[str]:
    """The names Ollama has pulled, from its own list."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base}/api/tags", timeout=TAGS_TIMEOUT)
        response.raise_for_status()
        models = (response.json() or {}).get("models") or []
        return [str(m.get("name") or "") for m in models if isinstance(m, dict)]


def _cli_problem(model: str) -> str | None:
    """Why a `cli.*` model cannot answer: its CLI is not on this computer."""
    base, _ = split_cli_model(model)
    if base not in CLI_BINARIES or cli_status.binary(base):
        return None
    label = cli_status.LABELS.get(base, base)
    return f"{label} is not installed on this computer (no `{CLI_BINARIES[base]}` in PATH)."


def _pulled(model: str, names: list[str]) -> bool:
    # Ollama reads a bare name as its `latest` tag, and nothing else.
    return model in names or (":" not in model and f"{model}:latest" in names)


async def model_problem(settings: dict, model: str) -> str | None:
    """Why `model` cannot answer, in a sentence; None when nothing is known to stop it."""
    return (await model_problems(settings, [model]))[model]


async def model_problems(settings: dict, models: list[str]) -> dict[str, str | None]:
    """`model_problem` for each of `models`, reading Ollama's list once for them all.

    A workflow's steps can name several models (F313); they are checked
    together, and the one local call is made only if one of them is Ollama's.
    """
    answers: dict[str, str | None] = {}
    local: list[str] = []
    for model in dict.fromkeys(models):
        provider = detect_provider_from_model(model)
        if provider in _KEYED:
            field, label = _KEYED[provider]
            answers[model] = None if (settings.get(field) or "").strip() else f"No {label} key is saved."
        elif provider in _ADDRESSED:
            field, label = _ADDRESSED[provider]
            answers[model] = None if (settings.get(field) or "").strip() else f"No {label} server address is saved."
        elif model.lower().startswith("cli."):
            answers[model] = _cli_problem(model)
        elif provider != "ollama":
            answers[model] = None
        else:
            local.append(model)
    if not local:
        return answers

    base = _ollama_base_url(settings)
    try:
        names = await _ollama_models(base)
    except httpx.HTTPStatusError as e:
        return answers | {m: f"Ollama at {base} answered {e.response.status_code}." for m in local}
    except Exception:
        return answers | {m: f"Ollama is not reachable at {base}." for m in local}
    for model in local:
        bare = model[len("ollama."):] if model.startswith("ollama.") else model
        answers[model] = None if _pulled(bare, names) else (
            f"Ollama has no model named {bare}. Pull it with `ollama pull {bare}`, or choose another model.")
    return answers
