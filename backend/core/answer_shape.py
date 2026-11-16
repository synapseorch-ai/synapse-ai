"""How a model step's declared answer is held, per model: one rung, from one table (F1618).

A step that declares its answer's shape (`output_fields`) is sent that shape as a JSON schema. How it is sent depends
on what the model holds, and that is decided here, once, beside `core.reasoning` and with the same split: the policy
(which model takes which rung) in this table, the request's shape in each adapter.

1. `schema`: the provider's own schema mode, which constrains the answer to the schema. Claude's
   `output_config.format` (on the API and, as `outputConfig.textFormat`, on Bedrock's Converse), OpenAI's strict
   `json_schema`, Gemini's `response_json_schema`, Ollama's `format`, a model CLI's own schema flag, and the model
   gateway, which takes OpenAI's form and passes it on.
2. `forced_tool`: one `answer` tool whose input is the schema, which the call must use, with thinking off (a forced
   tool and thinking don't go together). Only for an older Claude that has no schema mode, and never for the Claude
   models whose API refuses a forced `tool_choice` (they hold a schema anyway).
3. `prompt`: the answer's shape in the prompt's words alone, then validation in code and one repair naming the paths.

A refusal of the format itself (not supported, too complex, a key setup that has none) takes the next rung down,
once (`below`), and the provider writes a note the step says as a warning. A model's rung is never raised from a
guess: a model joins the table when its provider's page, or a shape check, says it holds a schema.

The Claude list is the structured-outputs page's own (read 2026-11-16): Fable 5, 5.1 and 5.2; Mythos 5, 5.1, 5.2 and
Preview; Opus 5.5, 5, 4.8, 4.7, 4.6 and 4.5; Sonnet 5.5, 5, 4.6 and 4.5; Haiku 5.5 and 4.5. Prefixes, longest first
where two could both match, so a model that ships in a family already here holds a schema the day it ships.
"""
from __future__ import annotations

SCHEMA = "schema"
FORCED_TOOL = "forced_tool"
PROMPT = "prompt"
RUNGS = (SCHEMA, FORCED_TOOL, PROMPT)

#: The Claude families with a schema mode (structured outputs), by prefix.
_CLAUDE_SCHEMA = (
    "claude-fable-5", "claude-mythos-5", "claude-mythos-preview",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
    "claude-haiku-5", "claude-haiku-4-5",
)
#: The Claude models whose API refuses a forced `tool_choice` with a 400: never on the forced-tool rung.
_REFUSE_A_FORCED_TOOL = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2",
                         "claude-mythos-5-1", "claude-mythos-5-2")
#: Bedrock's own prefix before a Claude id, in every geography an inference profile names.
_BEDROCK_CLAUDE = tuple(f"bedrock.{geo}anthropic." for geo in ("", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "global."))
#: Providers whose schema mode is already sent for every model they serve (F1553), and the gateway.
_SCHEMA_PROVIDERS = frozenset({"openai", "gemini", "ollama", "model_gateway"})
#: The model CLIs that take a schema flag.
_CLI_SCHEMA = ("cli.claude", "cli.codex")

#: Words in a refusal that name the format itself, never the key, the model or the request.
_FORMAT_REFUSED = ("schema is too complex", "too complex", "output_config", "outputconfig", "textformat",
                   "json_schema", "response_format", "structured output", "customer-managed", "cmek",
                   "unknown option '--json-schema'", "unexpected argument '--output-schema'",
                   "unknown option: --json-schema", "unrecognized arguments: --output-schema")
_NOT_SUPPORTED = ("not supported", "unsupported", "not available", "is not allowed", "unknown option",
                  "unexpected argument", "unrecognized")


def claude_id(model: str) -> str | None:
    """The Claude id in `model` (`claude-sonnet-4-6…`), without Bedrock's prefix; None for any other model."""
    name = (model or "").strip().lower()
    for prefix in _BEDROCK_CLAUDE:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name if name.startswith("claude-") else None


def _claude_rung(claude: str) -> str:
    if claude.startswith(_CLAUDE_SCHEMA):
        return SCHEMA
    if claude.startswith(_REFUSE_A_FORCED_TOOL):
        return PROMPT  # unreachable while every refuser holds a schema; never the forced tool
    return FORCED_TOOL


def rung(provider: str, model: str) -> str:
    """The one rung `model`, served by `provider` (the provider table's id), takes."""
    provider = (provider or "").strip().lower()
    name = (model or "").strip().lower()
    if name.startswith("cli."):
        return SCHEMA if name.startswith(_CLI_SCHEMA) else PROMPT
    if provider in _SCHEMA_PROVIDERS:
        return SCHEMA
    claude = claude_id(name)
    if provider in ("anthropic", "bedrock") and claude:
        return _claude_rung(claude)
    return PROMPT


def below(provider: str, model: str, current: str) -> str:
    """The rung a refused format falls to: always the words, from either rung above them. A model on the schema
    rung never falls to the forced tool, which the newest Claude models refuse and the rest hold no better."""
    return PROMPT


def refused(text: str) -> bool:
    """Whether a provider's refusal names the format itself (to step down), not the key, the model or the request."""
    said = (text or "").lower()
    if "too complex" in said or "customer-managed" in said or "cmek" in said:
        return True
    return any(w in said for w in _FORMAT_REFUSED) and any(w in said for w in _NOT_SUPPORTED + ("too complex",))


def stepped_down(provider_label: str, why: str) -> str:
    """The note a provider writes when it asked for the answer in words after a refusal of its schema mode."""
    return (f"The answer was asked for in words: {provider_label} refused its schema mode ({why.strip()[:160]}). It was "
            "checked in code as before.")
