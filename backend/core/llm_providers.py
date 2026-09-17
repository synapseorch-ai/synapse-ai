"""
LLM provider API callers (OpenAI, Anthropic, Gemini, Bedrock, Ollama, Grok, DeepSeek, CLI).
Extracted from server.py to eliminate duplication between chat() and chat_stream().

All cloud providers follow the same failsafe pattern:
  - 5 attempts max
  - Exponential backoff: [5, 10, 20, 40, 80] seconds
  - Raises LLMError on final failure (never returns error strings silently)

CLI providers (cli.claude, cli.gemini, cli.codex) use locally authenticated CLI sessions.
No API key needed — tool calling is emulated via system prompt injection + <tool_call> parsing.
"""
import os
import re
import json
from typing import NamedTuple
import tempfile
import asyncio
import base64
import threading
import httpx
import boto3
import botocore.exceptions
from botocore import UNSIGNED as _BEDROCK_UNSIGNED
from botocore.config import Config

from core.config import LLM_REQUEST_TIMEOUT

from core.log import get_logger

_log = get_logger(__name__)


def _retry_notice(provider: str, reason: str, attempt: int, max_retries: int, backoff: float) -> None:
    """Say, out loud, that a model call failed and is being tried again.

    Every retry loop below used to log "Retrying in Ns" at `debug` and nothing
    else — invisible at a worker's INFO level, and printed on the final attempt
    too, promising a retry that never came. A call that timed out and was
    retried was therefore a stretch of silence on the run page: 187 seconds of
    it in one report, on a run that then finished normally.

    Now a warning, and a notice for whoever is waiting (`core.model_wait`), but
    only when another attempt is actually coming.
    """
    if attempt >= max_retries:
        return
    _log.warning(f"⏳ {provider} {reason} on attempt {attempt}/{max_retries}; retrying in {backoff}s")
    from core.model_wait import report

    report(f"The model {reason} — retrying ({attempt + 1} of {max_retries}) in {backoff}s.")

# No process-level AWS_BEARER_TOKEN_BEDROCK. It used to be set here and never
# cleared, so one tenant's Bedrock key stayed in the environment of a worker
# that then ran another tenant's job — visible in /proc/<pid>/environ and
# inherited by every stdio MCP server and sandbox the worker spawned. The
# bearer token is attached per client instead, by the request handler at
# `_bedrock_bearer_auth` below, which was already the mechanism for the
# non-boto3 path.


# ─── CLI auth-failure detection ──────────────────────────────────────────────
# Specific, word-boundaried phrases only. A bare "auth" substring (the old
# behavior) false-positives on normal model output containing "authority",
# "authorization", "authorized", "author", etc. — which aborted otherwise
# successful CLI runs. Keep this list specific enough that it only matches real
# authentication failures, not incidental words in a model's response.
_AUTH_RE = re.compile(
    r"(?:"
    r"not logged in"
    r"|please log in"
    r"|log in to"
    r"|sign in to"
    r"|authentication required"
    r"|not authenticated"
    r"|authentication failed"
    r"|invalid api key"
    r"|expired api key"
    r"|api key (?:is )?(?:invalid|expired|required)"
    r"|session (?:has )?expired"
    r"|expired session"
    r"|\bunauthorized\b"
    r"|\b401\b"
    r"|/login"
    r")",
    re.IGNORECASE,
)


def _match_auth(text: str) -> bool:
    """Return True only if `text` contains a specific auth-failure phrase.

    Deliberately avoids bare substrings like "auth"/"login"/"expired" so normal
    model output ("authority", "authorization", ...) is never flagged.
    """
    return bool(text) and _AUTH_RE.search(text) is not None


# ─── Image helpers ──────────────────────────────────────────────────────────────

def _parse_data_uri(data_uri: str) -> tuple[str, str]:
    """Parse a data URI into (mime_type, raw_base64).

    Delegates to `core.images`, which is where the rules live now. What this
    used to do: take the header's word for the type and hand it to Anthropic as
    `media_type` — a caller-controlled string reaching a provider unchecked —
    assume `image/png` for anything without a header, and raise `ValueError`
    from inside the ReAct loop on a `data:` string with no comma.

    Requests arriving through `ChatRequest` are validated before they get here,
    so in practice this is the second lock rather than the first.
    """
    from core.images import ALLOWED_MIME, parse_data_uri

    mime, b64 = parse_data_uri(data_uri)
    return (mime if mime in ALLOWED_MIME else "image/png"), b64


def _build_openai_image_content(text: str, images: list[str] | None) -> list[dict] | str:
    """Build OpenAI/Grok multimodal content blocks.

    If images is empty, returns plain text string.
    Otherwise returns a list of content parts (text + image_url blocks).
    """
    if not images:
        return text
    # An image on its own is a message. Every provider here rejects an empty
    # text block, so the part is omitted rather than sent blank — which is what
    # made the old "[Images attached]" placeholder necessary.
    parts: list[dict] = [{"type": "text", "text": text}] if text and text.strip() else []
    for img in images[:5]:
        mime, b64 = _parse_data_uri(img)
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        })
    return parts


def _build_anthropic_image_content(text: str, images: list[str] | None) -> list[dict] | str:
    """Build Anthropic multimodal content blocks.

    Returns list of content parts with image + text blocks.
    """
    if not images:
        return text
    parts: list[dict] = []
    for img in images[:5]:
        mime, b64 = _parse_data_uri(img)
        parts.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64}
        })
    if text and text.strip():
        parts.append({"type": "text", "text": text})
    return parts


#: Cap on a single response. 4096 was hardcoded per provider; a long final
#: answer or a big tool-call argument hit it and came back truncated, which the
#: loop could not see. Overridable per tenant via the `max_output_tokens`
#: setting, read in `generate_response`.
DEFAULT_MAX_OUTPUT_TOKENS = 8192


#: Providers spell "why the model stopped" four different ways. The loop cares
#: about one distinction — was this response complete? — so they are normalised
#: to: "stop" (finished), "max_tokens" (cut off mid-sentence), "tool_use"
#: (stopped to call a tool), or None (the provider did not say).
_STOP_REASONS = {
    # Anthropic
    "end_turn": "stop", "stop_sequence": "stop", "refusal": "stop",
    "max_tokens": "max_tokens", "tool_use": "tool_use",
    # OpenAI-compatible (openai, grok, deepseek, v1, gateway) + Ollama
    "stop": "stop", "length": "max_tokens", "tool_calls": "tool_use",
    "content_filter": "stop", "function_call": "tool_use",
    # Gemini (upper-cased in the SDK)
    "MAX_TOKENS": "max_tokens", "STOP": "stop", "SAFETY": "stop", "RECITATION": "stop",
    # Bedrock Converse
    "end_turn ": "stop", "max_tokens ": "max_tokens",
}


def _normalise_stop_reason(raw) -> str | None:
    """One vocabulary for every provider's stop reason. Unknown values map to
    None rather than a guess: the loop only acts on "max_tokens"."""
    if raw is None:
        return None
    name = getattr(raw, "name", None) or str(raw)
    return _STOP_REASONS.get(name) or _STOP_REASONS.get(name.strip().lower())


def _apply_openai_reasoning(payload: dict, reasoning) -> dict:
    """Put `reasoning_effort` on an OpenAI-shaped payload, if there is one.

    Every OpenAI-compatible endpoint spells this the same way, so one helper
    serves openai, grok, deepseek, the v1-compatible providers and our gateway.
    """
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning.openai_effort
    return payload


def _retry_without_reasoning(payload: dict, resp) -> bool:
    """Drop `reasoning_effort` after a 400 that complained about it.

    "OpenAI-compatible" is a claim about a URL shape, not a guarantee about a
    parameter list — and the same is true of a model behind that URL: a proxy
    may accept the field for one model and refuse it for the next. There is no
    capability endpoint to consult, so the refusal is the discovery. Drop the
    field and try once more, rather than failing a turn over a knob the person
    only expressed a preference with.

    Narrow on purpose: only a 400, only when we actually sent the field, and
    only when the body names it. A 400 about anything else must still surface
    as the error it is.
    """
    if "reasoning_effort" not in payload:
        return False
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001 — a body we cannot read is not a verdict
        return False
    if "reasoning" not in body.lower():
        return False
    payload.pop("reasoning_effort", None)
    _log.debug("provider refused reasoning_effort — retrying without it")
    return True


class LLMResult(NamedTuple):
    """One provider call's outcome.

    The first five fields keep the order and meaning of the tuple every
    ``call_*`` used to return, so ``text, *_ = await call_x(...)`` and
    ``result[0]`` still read correctly. What is new is `stop_reason` — without
    it a response cut off at the token limit was indistinguishable from a
    finished one, and the loop treated it as the agent's final answer — and
    `tool_calls`, which is populated but not yet read (the loop still parses
    them out of the text; see the plan's Phase C).

    `reasoning` is the model's own thinking, from the provider's own channel —
    not the `[REASONING]` markers the system prompt asks for. Those still work
    and are still parsed, for the models that have nothing better; see
    `core.reasoning` for which models do. Carrying it as a field rather than
    splicing it back into `text` is the point: the marker round-trip meant the
    loop had to find the thinking again in a string it had just built, and on a
    model that ignored the convention there was nothing to find.
    """
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None
    tool_calls: tuple = ()
    reasoning: str = ""


class LLMError(Exception):
    """Raised when an LLM call fails after all retries.

    This propagates through the orchestration engine so it can stop execution
    instead of silently passing error strings to the next node.
    """
    pass


# Configuration — read at call time. `settings` wins over the environment so a
# tenant's own ollama_base_url is used; the env var stays as the deployment-wide
# default. This used to be the other way round, with server startup copying one
# tenant's setting into os.environ for every tenant to pick up.
def _ollama_base_url(settings: dict | None = None) -> str:
    configured = (settings or {}).get("ollama_base_url") or ""
    return configured.strip() or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

OLLAMA_MODEL = "llama3"


_OPENAI_REASONING_RE = re.compile(r"o\d")


def _is_openai_model(m: str) -> bool:
    """`gpt*`, `chatgpt*`, and the o-series reasoning models (`o1`, `o3-mini`,
    `o4-mini`…). Only `gpt*` used to count, so the o-series fell through to
    Ollama (F62). `o` + a digit, not a bare `o`, so `orca-mini` stays local."""
    return m.startswith(("gpt", "chatgpt")) or bool(_OPENAI_REASONING_RE.match(m))


#: OpenAI ids that are not chat-completions models. Audio, image, realtime,
#: speech, transcription, moderation, search and embedding models have their own
#: endpoints, and the `-pro`, `codex` and `deep-research` models are served only
#: by the Responses API — `call_openai` posts to chat/completions, so listing one
#: offers a model that fails on every call.
_OPENAI_NOT_CHAT_RE = re.compile(
    r"tts|transcribe|whisper|realtime|audio|image|dall-e|embedding|moderation|search"
    r"|instruct|codex|deep-research|(?:^|-)pro(?:-|$)"
)


def is_openai_chat_model(model_id: str) -> bool:
    """An OpenAI model this engine can chat with — what the model list offers (F62).

    The same family test the router uses, so a listed model is always routed to
    OpenAI, minus the ids chat-completions cannot serve.
    """
    m = model_id.lower()
    return _is_openai_model(m) and not _OPENAI_NOT_CHAT_RE.search(m)


def detect_mode_from_model(model_name: str) -> str:
    """Detect the provider mode from a model name prefix.

    Returns 'cloud' for OpenAI/Anthropic/Gemini/Grok/DeepSeek models,
    'bedrock' for Bedrock, 'cli' for local CLI session models (cli.*),
    and 'local' for anything else (assumed to be Ollama).
    """
    if not model_name:
        return "local"
    m = model_name.lower()
    if m.startswith("cli."):
        return "cli"
    if _is_openai_model(m):
        return "cloud"
    if m.startswith("claude"):
        return "cloud"
    if m.startswith("gemini") or m.startswith("gemma") or m.startswith("lyria"):
        return "cloud"
    if m.startswith("bedrock"):
        return "bedrock"
    if m.startswith("grok"):
        return "cloud"
    if m.startswith("deepseek"):
        return "cloud"
    if m.startswith("synapse."):
        return "cloud"
    if m.startswith("oaic."):
        return "cloud"
    if m.startswith("locv1."):
        return "local"
    if m.startswith("ollama."):
        return "local"
    if m.startswith("hf."):
        return "hf"
    return "local"


def detect_provider_from_model(model_name: str) -> str:
    """Detect the provider name from a model name prefix."""
    if not model_name:
        return "ollama"
    m = model_name.lower()
    # CLI session providers
    if m.startswith("cli.claude"):
        return "anthropic_cli"
    if m.startswith("cli.gemini"):
        return "gemini_cli"
    if m.startswith("cli.codex"):
        return "codex_cli"
    if m.startswith("cli.copilot"):
        return "github_copilot_cli"
    if m.startswith("cli."):
        return "cli"
    if _is_openai_model(m):
        return "openai"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini") or m.startswith("gemma") or m.startswith("lyria"):
        return "gemini"
    if m.startswith("bedrock"):
        return "bedrock"
    if m.startswith("grok"):
        return "grok"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("synapse."):
        return "model_gateway"
    if m.startswith("oaic."):
        return "openai_compatible"
    if m.startswith("locv1."):
        return "local_compatible"
    if m.startswith("ollama."):
        return "ollama"
    if m.startswith("hf."):
        return "huggingface"
    return "ollama"



def _normalize_bedrock_api_key(settings: dict) -> str:
    """Extract and normalize the Bedrock API key from settings.

    Strips surrounding quotes and common header prefixes (Authorization: Bearer ...)
    that users often paste. Both ABSK... and bedrock-api-key... formats are returned
    as-is after stripping prefixes.
    """
    api_key = (settings.get("bedrock_api_key") or "").strip()
    if not api_key:
        return ""
    if (api_key.startswith('"') and api_key.endswith('"')) or (
        api_key.startswith("'") and api_key.endswith("'")
    ):
        api_key = api_key[1:-1].strip()
    lower = api_key.lower()
    if lower.startswith("authorization:"):
        api_key = api_key.split(":", 1)[1].strip()
        lower = api_key.lower()
    if lower.startswith("bearer "):
        api_key = api_key.split(" ", 1)[1].strip()
    return api_key


def _make_aws_client(service_name: str, region: str, settings: dict):
    """Create a boto3 client.

    If access/secret are not provided, boto3 will use its default credential chain
    (env vars, AWS_PROFILE, SSO, instance role, etc.).
    """
    # Amazon Bedrock API keys can be provided as a bearer token via this env var.
    # See: https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html
    bedrock_api_key = _normalize_bedrock_api_key(settings)

    # If a Bedrock API key is provided, prefer it and avoid mixing auth mechanisms.
    if bedrock_api_key:
        access_key = ""
        secret_key = ""
        session_token = ""
    else:
        access_key = (settings.get("aws_access_key_id") or "").strip()
        secret_key = (settings.get("aws_secret_access_key") or "").strip()
        session_token = (settings.get("aws_session_token") or "").strip()
    region_name = (region or settings.get("aws_region") or "us-east-1").strip()

    kwargs = {
        "service_name": service_name,
        "region_name": region_name,
    }

    if access_key and secret_key:
        kwargs.update(
            {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
            }
        )
        if session_token:
            kwargs["aws_session_token"] = session_token

    # -------------------------------------------------------------------------
    # RETRY CONFIGURATION (Fix for ServiceUnavailableException / Throttling)
    # -------------------------------------------------------------------------
    # Standard retries are often insufficient for high-concurrency Bedrock usage.
    # Adaptive mode allows standard retry logic to dynamically adjust for
    # optimal request rates.
    config_kwargs: dict = {
        "retries": {"max_attempts": 10, "mode": "adaptive"},
        "read_timeout": 900,
        "connect_timeout": 900,
    }
    if bedrock_api_key:
        # UNSIGNED, not merely "no credentials configured". botocore would
        # otherwise run SigV4 and raise NoCredentialsError *before* the
        # before-send hook that attaches the bearer token ever fires — which is
        # precisely the deployment this change is for, where no AWS credentials
        # exist in the environment at all. Matches the pre-existing bearer path
        # at `_invoke_bedrock_with_bearer`.
        config_kwargs["signature_version"] = _BEDROCK_UNSIGNED
    kwargs["config"] = Config(**config_kwargs)

    client = boto3.client(**kwargs)

    if bedrock_api_key:
        # Sign this client's requests with the tenant's bearer token, rather
        # than putting it in the process environment for botocore to find.
        # Scoped to the client, so two tenants' clients cannot see each other's
        # key no matter how their calls interleave.
        client.meta.events.register(
            "before-send.bedrock-runtime.*",
            _bearer_auth_handler(bedrock_api_key),
        )
    return client


def _bearer_auth_handler(api_key: str):
    """A botocore before-send hook that replaces SigV4 with a bearer token."""
    def _handler(request, **_kwargs):
        request.headers["Authorization"] = f"Bearer {api_key}"
        return None
    return _handler


# ─── CLI Session Providers ──────────────────────────────────────────────────────

# Maps cli.* model names to their CLI binary + flags
_CLI_COMMANDS: dict[str, list[str]] = {
    "cli.claude":   ["claude", "-p", "--allowedTools", ""],
    "cli.gemini":   ["gemini", "--prompt", ""],
    # `instructions` (codex's system-prompt slot) is set per-call in
    # call_cli_provider — never left empty, since some accounts reject an empty
    # value with 400 "Instructions are required".
    "cli.codex":    ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                     "-c", "features.shell_tool=false"],
    "cli.copilot":  ["copilot", "-p"],
}

# Seconds to wait for a CLI process before giving up
_CLI_TIMEOUT = 300.0


def _build_cli_prompt(
    sys_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> str:
    """Build a single flat text prompt for CLI providers.

    Combines system prompt (with optional tool schema injection) + conversation
    transcript into one string that can be piped into a CLI via stdin.
    """
    parts: list[str] = []

    # 1. System prompt
    if sys_prompt and sys_prompt.strip():
        parts.append(sys_prompt.strip())

    # 2. Tool schema injection — emulates native tool calling
    if tools:
        import json as _json
        tool_block = (
            "\n\n===== AVAILABLE TOOLS =====\n"
            "You have access to the following tools. "
            "To call a tool, output EXACTLY the following XML block (nothing before or after it on that response):\n"
            "<tool_call>{\"tool\": \"<tool_name>\", \"arguments\": {<args>}}</tool_call>\n\n"
            "Tools:\n"
        )
        for t in tools:
            func = t.get("function", {})
            name = func.get("name", "")
            if not name:
                continue
            desc = func.get("description", "")
            params = func.get("parameters", {})
            tool_block += f"- {name}: {desc}\n"
            if params.get("properties"):
                tool_block += f"  Parameters: {_json.dumps(params['properties'])}\n"
        tool_block += "===========================\n"
        parts.append(tool_block)

    # 3. Conversation history transcript
    transcript = _messages_to_transcript(messages)
    if transcript:
        parts.append(transcript)

    return "\n\n".join(parts)


async def call_cli_provider(
    cli_model: str,
    full_prompt: str,
    sys_prompt: str | None = None,
    messages: list[dict] | None = None,
    timeout: float = _CLI_TIMEOUT,
    cli_session_id: str | None = None,
) -> tuple[str, int, int, str | None]:
    """Spawn a local CLI binary, feed it the full context, and return the response.

    Args:
        cli_model: One of 'cli.claude', 'cli.gemini', 'cli.codex', 'cli.copilot'
        full_prompt: Complete flat-text prompt (system + tools + transcript)
        timeout: Seconds before we kill the process and raise LLMError
        cli_session_id: Existing CLI session ID to resume (passed via --resume).

    Returns:
        (response_text, estimated_input_tokens, estimated_output_tokens, new_session_id)

    Raises:
        LLMError: on timeout, auth failure, binary not found, or empty output
    """
    from core import usage_tracker

    ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    # Auth-failure detection uses the module-level _match_auth() helper (specific
    # phrases only). Rate-limit detection keeps its broad substring list below.
    RATE_LIMIT_PATTERNS = ["429", "rate limit", "ratelimit", "quota", "too many requests", "capacity", "resource_exhausted", "ratelimitexceeded"]

    # Split into base ("cli.<provider>") + variant, preserving dots in the
    # variant (e.g. "cli.codex.gpt-5.4" → variant "gpt-5.4"). The base is matched
    # case-insensitively; the variant is kept verbatim since model names can be
    # case-sensitive and are passed straight through to the CLI's -m/--model flag.
    parts = cli_model.split(".", 2)
    if len(parts) >= 2:
        base_cli = f"{parts[0]}.{parts[1]}".lower()
    else:
        base_cli = cli_model.lower()
    variant = parts[2] if len(parts) > 2 else None

    base_cmd = _CLI_COMMANDS.get(base_cli)
    if not base_cmd:
        raise LLMError(f"Unknown CLI base '{base_cli}'. Supported: {list(_CLI_COMMANDS.keys())}")

    cmd = base_cmd.copy()

    # ── Session Resume ──
    # If a previous CLI session ID exists for this agent+session, continue it.
    if cli_session_id:
        cmd.extend(["--resume", cli_session_id])

    # ── Dynamic Model & Options Parsing ──
    if variant:
        if base_cli == "cli.claude":
            # Extract base model by removing any thinking suffixes
            clean_variant = variant.replace('-thinking', '').replace('-max', '').replace('-high', '')
            cmd.extend(["--model", clean_variant])

            # Thinking / Effort overrides
            if "-thinking" in variant or "-max" in variant:
                cmd.extend(["--effort", "medium"])
            elif "-high" in variant:
                cmd.extend(["--effort", "high"])
            else:
                cmd.extend(["--effort", "low"])

        elif base_cli == "cli.gemini":
            if "pro" in variant:
                cmd.extend(["--model", "pro"])
            elif "flash" in variant:
                cmd.extend(["--model", "flash"])

        elif base_cli == "cli.codex":
            # Pass the variant directly as the model name (e.g. o3, o4-mini, gpt-4o)
            cmd.extend(["-m", variant])

        elif base_cli == "cli.copilot":
            # Pass the variant directly as the model name (e.g. claude-sonnet-4-5, gpt-4o)
            cmd.extend(["--model", variant])

    # codex exec requires "-" as the positional PROMPT arg to signal stdin input.
    # Append it after all option flags are resolved so it stays last.
    if base_cli == "cli.codex":
        cmd.append("-")

    import shutil
    executable = shutil.which(cmd[0])
    if not executable:
        raise LLMError(f"CLI binary '{cmd[0]}' exactly resolved by shutil.which not found. Check your PATH.")

    env = os.environ.copy()
    temp_sys_prompt_file = None
    temp_input_file = None
    temp_output_file = None

    # Prompt and response files handed to a CLI subprocess and deleted after
    # the call. Genuinely throwaway, so: scratch. Kept off the system temp dir
    # for the cross-OS permission reasons that put them under the project root
    # in the first place.
    from core.storage.scratch import scratch_dir
    _tmp_dir = str(scratch_dir("llm_cli"))

    try:
        if base_cli == "cli.copilot":
            # copilot -p takes the full prompt inline as a CLI argument; stdin is not used.
            # System prompt is already embedded in full_prompt by _build_cli_prompt().
            cmd.append(full_prompt)
            stdin_source = asyncio.subprocess.DEVNULL
        else:
            if base_cli == "cli.claude" and sys_prompt:
                # Write sys_prompt to a temp file and pass via --system-prompt-file.
                # --no-tools (added above) ensures Claude Code's native tools are stripped so
                # only our XML emulation is active.
                temp_sys_prompt_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", prefix="claude_sys_prompt_", delete=False,
                    encoding="utf-8", dir=_tmp_dir
                )
                temp_sys_prompt_file.write(sys_prompt)
                temp_sys_prompt_file.flush()
                temp_sys_prompt_file.close()
                cmd.extend(["--system-prompt-file", temp_sys_prompt_file.name])

                # stdin carries only the content after the sys_prompt (tools + messages).
                # full_prompt = sys_prompt.strip() + "\n\n" + rest, so strip the prefix.
                prefix = sys_prompt.strip()
                stdin_payload = full_prompt[len(prefix):].lstrip("\n") if full_prompt.startswith(prefix) else full_prompt

            elif base_cli == "cli.gemini" and sys_prompt:
                # Gemini CLI reads GEMINI_SYSTEM_MD env var as the path to a markdown file
                # containing the system prompt, overriding its native default system prompt.
                temp_sys_prompt_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", prefix="gemini_sys_prompt_", delete=False,
                    encoding="utf-8", dir=_tmp_dir
                )
                temp_sys_prompt_file.write(sys_prompt)
                temp_sys_prompt_file.flush()
                temp_sys_prompt_file.close()
                env["GEMINI_SYSTEM_MD"] = temp_sys_prompt_file.name

                # stdin carries only the content after the sys_prompt (tools + messages).
                prefix = sys_prompt.strip()
                stdin_payload = full_prompt[len(prefix):].lstrip("\n") if full_prompt.startswith(prefix) else full_prompt

            elif base_cli == "cli.codex":
                # Codex's `instructions` config is its system-prompt slot and must be
                # non-empty — an empty value 400s with "Instructions are required" on
                # some accounts. The agent's real (ReAct) system prompt continues to
                # ride in stdin via full_prompt, exactly as before; this just
                # satisfies the API with a neutral non-empty value.
                cmd.extend(["-c", "instructions=You are a helpful assistant."])
                stdin_payload = full_prompt

            else:
                stdin_payload = full_prompt

            # Write stdin payload to a temp file and feed it as stdin (like `< file`),
            # avoiding large string pipes and keeping content out of the process list.
            temp_input_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="cli_input_", delete=False,
                encoding="utf-8", dir=_tmp_dir
            )
            temp_input_file.write(stdin_payload)
            temp_input_file.flush()
            temp_input_file.close()

            # codex exec: use -o to capture only the final agent message, avoiding TUI noise
            if base_cli == "cli.codex":
                temp_output_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", prefix="codex_output_", delete=False,
                    encoding="utf-8", dir=_tmp_dir
                )
                temp_output_file.close()
                cmd.extend(["-o", temp_output_file.name])

            stdin_source = open(temp_input_file.name, "rb")

        _log.debug(f"🖥️  CLI provider '{cli_model}' — spawning subprocess: {' '.join(str(a) for a in cmd)}")

        process = await asyncio.create_subprocess_exec(
            executable,
            *cmd[1:],
            stdin=stdin_source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        if temp_input_file:
            stdin_source.close()

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            raise LLMError(
                f"CLI provider '{cli_model}' timed out after {timeout}s. "
                "Is the CLI authenticated and responsive?"
            )

        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()
        if stderr_text:
            # Scan only the tail of stderr for error patterns — Codex (and some other CLIs)
            # echo the stdin/banner to stderr, which can contain words like "capacity" that
            # would falsely trigger rate-limit detection. Real API errors appear at the end.
            lower_err = stderr_text[-600:].lower()
            if any(p in lower_err for p in RATE_LIMIT_PATTERNS):
                raise LLMError(
                    f"CLI provider '{cli_model}' rate limit / capacity error.\n"
                    f"Details: {stderr_text[:400]}"
                )
            if _match_auth(stderr_text[-600:]):
                raise LLMError(
                    f"CLI provider '{cli_model}' authentication failure detected.\n"
                    f"Run the CLI in your terminal to re-authenticate.\n"
                    f"Details: {stderr_text[:400]}"
                )
            _log.debug(f"🖥️  CLI stderr: {stderr_text[:300]}")

        # codex exec writes just the final agent message to the -o file; use that
        # to avoid parsing the TUI header/footer noise from stdout.
        if base_cli == "cli.codex" and temp_output_file:
            try:
                with open(temp_output_file.name, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except Exception:
                raw = stdout_b.decode("utf-8", errors="replace")
        else:
            raw = stdout_b.decode("utf-8", errors="replace")
        clean = ANSI_ESCAPE.sub("", raw).strip()

        # ── Session ID extraction & response unwrapping ──
        new_session_id: str | None = None
        if base_cli == "cli.claude":
            # Claude CLI with --output-format json returns a structured object.
            # Extract result text and session_id from it.
            try:
                import json as _json
                obj = _json.loads(clean)
                new_session_id = obj.get("session_id")
                extracted = obj.get("result", "")
                if isinstance(extracted, str) and extracted.strip():
                    clean = extracted.strip()
                # Propagate JSON-level auth errors before the generic check below.
                # Gate on the structured is_error flag and scan only the message
                # fields — not the stringified whole object — so unrelated keys
                # can't trip a false positive.
                err_text = " ".join(str(obj.get(k, "")) for k in ("result", "error", "subtype"))
                if obj.get("is_error") and _match_auth(err_text):
                    raise LLMError(
                        f"CLI provider '{cli_model}' authentication failure.\n"
                        f"Run 'claude' in your terminal to re-authenticate.\n"
                        f"Details: {str(obj)[:300]}"
                    )
            except LLMError:
                raise
            except Exception:
                pass  # not JSON or parse error — treat clean as plain text
        else:
            # Best-effort session ID extraction for gemini, codex, copilot
            sid_match = re.search(r"session[_\-]?id[:\s]+([a-zA-Z0-9_\-]{8,})", clean, re.IGNORECASE)
            new_session_id = sid_match.group(1) if sid_match else None

        # Auth failure check (also catches plain-text "Not logged in" from claude).
        # `clean` is the model's normal stdout on success, so only treat it as an
        # auth failure when the process ALSO exited non-zero — a successful (exit-0)
        # response is a valid answer, never an auth error, even if it happens to
        # mention words like "authorization". Genuine auth failures exit non-zero
        # (or, for claude JSON, are caught by the is_error block above).
        if process.returncode != 0 and _match_auth(clean):
            raise LLMError(
                f"CLI provider '{cli_model}' is not authenticated.\n"
                f"Run the CLI in your terminal and complete the login flow, then retry.\n"
                f"Details: {clean[:300]}"
            )

        if not clean:
            # Report the TAIL of stderr, not the head. CLIs like codex echo a
            # startup banner (version/workdir/model) to the head of stderr and
            # write the actual failure (e.g. an invalid_request_error for an
            # unsupported model) at the tail — so [:200] would hide the real cause.
            raise LLMError(
                f"CLI provider '{cli_model}' returned empty output. "
                f"Return code: {process.returncode}. "
                f"stderr: {stderr_text[-600:]}"
            )

        _log.debug(f"✅ CLI '{cli_model}' response ({len(clean)} chars)")

        input_est = usage_tracker.estimate_tokens_from_text(full_prompt)
        output_est = usage_tracker.estimate_tokens_from_text(clean)
        return clean, input_est, output_est, new_session_id

    except LLMError:
        raise
    except FileNotFoundError:
        raise LLMError(
            f"CLI binary for '{cli_model}' not found in PATH. "
            f"Install and authenticate the CLI first: {cmd[0]}"
        )
    except Exception as e:
        raise LLMError(f"CLI provider '{cli_model}' unexpected error: {e}")
    finally:
        for f in [temp_sys_prompt_file, temp_input_file, temp_output_file]:
            if f:
                try:
                    os.unlink(f.name)
                except Exception:
                    pass


# ────────────────────────────────────────────────────────────────────────────────

async def call_openai(model, messages, api_key, tools=None, images=None, prompt_cache: bool = True,
                      reasoning=None):
    """Call OpenAI with 5-attempt exponential backoff retry loop.

    OpenAI prompt caching is automatic for stable prefixes ≥1024 tokens; we
    just need to extract `usage.prompt_tokens_details.cached_tokens` from the
    response so the savings are visible in usage logs.

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        — `input_tokens` is cache-miss tokens; `cache_read_tokens` is reused-prefix tokens.
    """
    # Inject images into the last user message
    if images:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i] = dict(messages[i])  # shallow copy
                messages[i]["content"] = _build_openai_image_content(
                    str(messages[i].get("content", "")), images
                )
                break
    OPENAI_TIMEOUT = LLM_REQUEST_TIMEOUT
    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]

    payload: dict = {"model": model, "messages": messages}
    _apply_openai_reasoning(payload, reasoning)
    if tools:
        # OpenAI uses the same format as our internal Ollama tool spec
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            _log.debug(f"🔄 OpenAI call start (attempt {attempt}/{MAX_RETRIES})")
            async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            if resp.status_code in (429, 499, 500, 502, 503, 529):
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                _retry_notice("OpenAI", f"answered {resp.status_code}", attempt, MAX_RETRIES, backoff)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    continue
            if resp.status_code == 400 and _retry_without_reasoning(payload, resp):
                continue
            resp.raise_for_status()
            data = resp.json()
            # Extract actual token usage from the API response
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            from core.cache.prompt_cache import extract_openai_cache_tokens
            cache_read, cache_write = extract_openai_cache_tokens(usage) if prompt_cache else (0, 0)
            # OpenAI reports prompt_tokens INCLUSIVE of cached tokens; subtract so
            # `input_tokens` is cache-miss only for consistent billing math.
            if cache_read:
                input_tokens = max(0, input_tokens - cache_read)
            # Handle tool_calls response
            choice = data["choices"][0]
            msg = choice.get("message", {})
            text, _reasoning = _openai_compat_extract(msg)
            _log.debug(f"✅ OpenAI call complete (attempt {attempt})")
            return LLMResult(text, input_tokens, output_tokens, cache_read, cache_write,
                             _normalise_stop_reason(choice.get("finish_reason")), (), _reasoning)
        except httpx.TimeoutException:
            last_error = f"Request timed out ({OPENAI_TIMEOUT}s)"
            _retry_notice("OpenAI", f"did not answer within {OPENAI_TIMEOUT:g}s", attempt, MAX_RETRIES, backoff)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {str(e)[:200]}"
            _log.warning(f"❌ OpenAI HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES and e.response.status_code in (429, 499, 500, 502, 503, 529):
                await asyncio.sleep(backoff)
                continue
            break
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ OpenAI error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue

    error_msg = f"OpenAI LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)

def _convert_tools_for_anthropic(ollama_tools: list[dict] | None) -> list[dict] | None:
    """Convert Ollama-format tool list to Anthropic tool format.

    Ollama format:
      [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
    Anthropic format:
      [{"name": ..., "description": ..., "input_schema": ...}]
    """
    if not ollama_tools:
        return None

    tools = []
    for t in ollama_tools:
        func = t.get("function", {})
        name = func.get("name", "")
        if not name:
            continue
        tool_def = {
            "name": name,
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        }
        tools.append(tool_def)

    return tools if tools else None


def _openai_compat_extract(msg: dict) -> tuple[str, str]:
    """Normalize an OpenAI-compatible chat-completion message into
    `({"tool": ..., "arguments": ...}` shape or plain text, reasoning)`.

    **`reasoning_content` was being dropped, and it is the interesting half.**
    Every reasoning model behind an OpenAI-compatible endpoint — DeepSeek-R1,
    anything on vLLM, gpt-oss through our own gateway — returns its thinking in
    a sibling field to `content`, unprompted. This function read `content` and
    nothing else, so on those models the thinking panel was empty while the
    model was demonstrably thinking. Some endpoints spell it `reasoning`
    instead, so both are read.

    Narration beside a tool call is still treated as reasoning, as before, and
    appended after the real thinking when a model sends both.
    """
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        # Every call, as consecutive JSON objects: the ReAct loop parses all of
        # them and executes them in order. Only `tool_calls[0]` used to
        # survive, so a model that answered with two parallel calls ran one.
        call_json = "\n".join(json.dumps({
            "tool": tc["function"]["name"],
            "arguments": json.loads(tc["function"].get("arguments") or "{}"),
        }) for tc in tool_calls)
        narration = (msg.get("content") or "").strip()
        if narration:
            reasoning = f"{reasoning}\n\n{narration}".strip() if reasoning else narration
        return call_json, reasoning
    return (msg.get("content", "") or ""), reasoning


def _extract_anthropic_response(response) -> tuple[str, str]:
    """Split an Anthropic SDK response into (text, reasoning).

    Two sources of reasoning, and they are not the same thing. A `thinking`
    block is extended thinking — the model's real scratchpad, returned because
    the request asked for it. A `text` block sitting before a `tool_use` block
    is Claude narrating its call, which it does routinely with or without
    extended thinking enabled.

    Both belong in the thinking panel, and when both are present the real
    thinking comes first. `redacted_thinking` is deliberately skipped rather
    than shown: it is an encrypted blob, not prose, and rendering it would put
    a wall of base64 where an explanation should be.
    """
    if not response.content:
        return "Error: Empty Anthropic response.", ""

    thinking_parts: list[str] = []
    text_parts: list[str] = []
    tool_use_calls: list[dict] = []
    for block in response.content:
        if block.type == "tool_use":
            tool_use_calls.append({"tool": block.name, "arguments": block.input or {}})
        elif block.type == "thinking":
            thought = getattr(block, "thinking", "")
            if thought:
                thinking_parts.append(thought)
        elif block.type == "text" and block.text:
            text_parts.append(block.text)

    reasoning = "\n".join(t for t in thinking_parts if t and t.strip()).strip()
    answer = "\n".join(t for t in text_parts if t and t.strip()).strip()

    if tool_use_calls:
        # Every call, as consecutive JSON objects — the loop executes them in
        # order. Only the first tool_use block used to survive.
        call_json = "\n".join(json.dumps(c) for c in tool_use_calls)
        if answer:
            # Narration beside a call is reasoning, and it reads after the real
            # thinking rather than replacing it.
            reasoning = f"{reasoning}\n\n{answer}".strip() if reasoning else answer
        return call_json, reasoning

    if answer:
        return answer, reasoning

    if reasoning:
        return "Error: Anthropic returned only reasoning — raise the response cap or lower the reasoning effort.", reasoning

    return "Error: Anthropic returned no usable content.", ""


async def call_anthropic(model, messages, system, api_key, tools=None, images=None, prompt_cache: bool = True,
                         prompt_blocks: list[str] | None = None, volatile_tail: str | None = None,
                         max_tokens: int | None = None, reasoning=None):
    """Call Anthropic using the official SDK with native tool calling.

    Args:
        model: Claude model name (e.g. 'claude-sonnet-4-20250514')
        messages: List of {"role": "user"/"assistant", "content": "..."} dicts
        system: System instruction text
        api_key: Anthropic API key
        tools: Ollama-format tool list (converted to Anthropic tool definitions)
        images: List of base64 data-URI image strings to attach to the last user message
        prompt_cache: When True (default), mark the system prompt + tools as a cache
                      breakpoint via cache_control. Adds ~25% to write cost but yields
                      ~90% off subsequent reads.
        prompt_blocks: The last user message as one text block per transcript turn.
                      When given, the last block carries a third cache marker and
                      `volatile_tail` (per-turn text) follows it unmarked, so the
                      transcript itself is a cache hit turn over turn. The joined
                      text of blocks + tail is what `messages[-1]["content"]`
                      already holds; this is the same text, structured.

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        — `input_tokens` is the cache-miss prompt tokens only; cache reads/writes are
        separated so usage_tracker can apply the correct pricing tier.
    """
    # The last user message as content blocks: transcript turns, the cache
    # marker on the last of them, then the volatile tail. Built before the
    # images so an image-bearing first turn prepends to the block list rather
    # than stringifying it.
    if prompt_blocks and messages and messages[-1].get("role") == "user":
        from core.cache.prompt_cache import build_anthropic_user_content
        tools_chars = len(str(tools)) if tools else 0
        prefix_chars = len(str(system or "")) + tools_chars + sum(len(b) for b in prompt_blocks)
        messages = list(messages)
        messages[-1] = dict(messages[-1])
        messages[-1]["content"] = build_anthropic_user_content(
            prompt_blocks, volatile_tail,
            prefix_chars=prefix_chars if prompt_cache else 0,
        )

    # Inject images into the last user message
    if images:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i] = dict(messages[i])  # shallow copy
                existing = messages[i].get("content", "")
                if isinstance(existing, list):
                    image_blocks = _build_anthropic_image_content("", images)
                    messages[i]["content"] = [b for b in image_blocks if b.get("type") == "image"] + existing
                else:
                    messages[i]["content"] = _build_anthropic_image_content(str(existing), images)
                break
    import anthropic

    ANTHROPIC_TIMEOUT = LLM_REQUEST_TIMEOUT  # seconds per attempt (Claude can take >60s for complex prompts)
    MAX_RETRIES = 5

    # --- Input validation to prevent 400 errors ---
    # 1. Filter out messages with empty content
    clean_messages = [
        m for m in (messages or [])
        if m.get("content") and str(m["content"]).strip()
    ]
    # 2. Ensure messages start with "user" role (Claude requirement)
    while clean_messages and clean_messages[0].get("role") != "user":
        clean_messages.pop(0)
    # 3. If no valid messages remain, create a minimal one
    if not clean_messages:
        clean_messages = [{"role": "user", "content": "Hello"}]

    # Convert tools
    anthropic_tools = _convert_tools_for_anthropic(tools)

    # Build kwargs
    kwargs = {
        "model": model,
        "messages": clean_messages,
        "max_tokens": max_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
    }
    if system and str(system).strip():
        kwargs["system"] = str(system).strip()
    if anthropic_tools:
        kwargs["tools"] = anthropic_tools
    if reasoning is not None:
        from core import reasoning as _reasoning_policy
        if _reasoning_policy.anthropic_thinking(model) == _reasoning_policy.ADAPTIVE:
            # Claude 5 and Opus/Sonnet 4.6+ (F9): a budget is a 400 or
            # deprecated there. "summarized", because the default display on
            # the newer models is an empty thinking block.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": reasoning.effort}
        else:
            # `max_tokens` above already carries the raised cap from
            # `core.reasoning.plan_for`, which is what keeps this valid: the API
            # refuses a budget that is not strictly below it.
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": reasoning.budget}

    # Decorate with prompt cache markers when enabled and the prefix is large enough.
    if prompt_cache:
        from core.cache.prompt_cache import decorate_anthropic_kwargs
        kwargs = decorate_anthropic_kwargs(kwargs, str(system).strip() if system else None)

    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=ANTHROPIC_TIMEOUT,
        max_retries=0,  # Disable SDK internal retries — we handle retries ourselves
    )

    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]  # seconds between retries
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            _log.debug(f"🔄 Anthropic call start (attempt {attempt}/{MAX_RETRIES})")
            response = await client.messages.create(**kwargs)
            _log.debug(f"✅ Anthropic call complete (attempt {attempt})")
            # Extract actual token usage from the SDK response object
            input_tokens = getattr(getattr(response, 'usage', None), 'input_tokens', 0) or 0
            output_tokens = getattr(getattr(response, 'usage', None), 'output_tokens', 0) or 0
            from core.cache.prompt_cache import extract_anthropic_cache_tokens
            cache_read, cache_write = extract_anthropic_cache_tokens(response)
            _text, _reasoning = _extract_anthropic_response(response)
            return LLMResult(_text, input_tokens, output_tokens,
                             cache_read, cache_write,
                             _normalise_stop_reason(getattr(response, "stop_reason", None)),
                             (), _reasoning)
        except anthropic.APITimeoutError:
            last_error = f"Request timed out ({ANTHROPIC_TIMEOUT}s)"
            _retry_notice("Anthropic", f"did not answer within {ANTHROPIC_TIMEOUT:g}s", attempt, MAX_RETRIES, backoff)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue
        except anthropic.APIStatusError as e:
            last_error = f"API error {e.status_code}: {str(e)[:200]}"
            _log.warning(f"❌ Anthropic API error {e.status_code} on attempt {attempt}/{MAX_RETRIES}: {str(e)[:500]}")
            # Retry on transient/rate-limit errors (429 rate limit, 499 client disconnect, 5xx server errors, 529 overloaded)
            if attempt < MAX_RETRIES and e.status_code in (429, 499, 500, 502, 503, 529):
                _retry_notice("Anthropic", f"answered {e.status_code}", attempt, MAX_RETRIES, backoff)
                await asyncio.sleep(backoff)
                continue
            # Non-retryable error (400, 401, 403, etc.) or last attempt
            break
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ Anthropic error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue

    error_msg = f"Claude LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)

def _convert_tools_for_gemini(ollama_tools: list[dict] | None):
    """Convert Ollama-format tool list to Gemini FunctionDeclaration list.

    Ollama format:
      [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
    Gemini format:
      types.Tool(function_declarations=[FunctionDeclaration(...)])
    """
    from google.genai import types

    if not ollama_tools:
        return None

    declarations = []
    for t in ollama_tools:
        func = t.get("function", {})
        name = func.get("name", "")
        if not name:
            continue
        # Clean up the parameters schema — Gemini doesn't accept 'default' in properties
        params = func.get("parameters", {})
        # Building the declaration is where validation lives, and it is validation
        # of the *whole list*: one tool the cleaner failed to tame used to raise
        # here and cost the agent every other tool it had, on every turn. The
        # cleaner has been rewritten four times by MCP servers declaring keywords
        # nobody had listed — `oneOf`, non-string `enum`, `exclusiveMinimum`, then
        # type unions — so there will be a fifth. Losing one capability quietly
        # beats losing all of them loudly.
        try:
            cleaned_params = _clean_schema_for_gemini(params) if params else None
            declarations.append(types.FunctionDeclaration(
                name=name,
                description=func.get("description", ""),
                parameters=cleaned_params,
            ))
        except Exception as e:
            _log.warning(
                f"⚠️ Gemini: dropping tool {name!r} — its schema could not be "
                f"converted: {str(e)[:300]}",
            )
            continue

    if not declarations:
        return None
    return [types.Tool(function_declarations=declarations)]


#: JSON Schema keywords `google.genai`'s `Schema` has no field for. Anything
#: here is dropped; the composition keywords are handled separately below,
#: because dropping one leaves a node with no type and Gemini rejects that too.
def _gemini_json_type(value) -> str:
    """The JSON Schema type name for a literal. `bool` before `int` — it is one."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _gemini_fixed_value(value) -> dict:
    """`const`, expressed in what Gemini's `Schema` can hold.

    A string const becomes a one-value `enum`, which says exactly what `const`
    says. Anything else keeps its type and states the fixed value in prose,
    because `enum` here is `list[str]` and coercing `true` into `"true"` changes
    what the model sends the tool.
    """
    if isinstance(value, str):
        return {"type": "string", "enum": [value]}
    return {
        "type": _gemini_json_type(value),
        "description": f"Must be {json.dumps(value)}.",
    }


#: Keys whose value is a map of name → schema rather than a schema.
_SCHEMA_MAPS = {"properties", "$defs", "definitions"}

#: Exactly what `google.genai`'s `Schema` accepts, by its JSON name.
#:
#: A whitelist, not a list of things to strip, and that is the point. The strip
#: list was rewritten three times in one sitting — `oneOf`, then non-string
#: `enum` values, then `exclusiveMinimum` — each time because a real server
#: declared something nobody had thought of, and each time the whole tool list
#: was rejected and the customer's agent answered with a pydantic traceback.
#: JSON Schema has a long tail and MCP servers use it; `Schema`'s fields are
#: finite and knowable. Filtering to what is accepted cannot be surprised.
#:
#: Regenerate the candidate set with:
#:     [f.alias or n for n, f in types.Schema.model_fields.items()]
#: then subtract `_GEMINI_NOT_IN_FUNCTION_SCHEMA` — see below, it is not the
#: same list.
_GEMINI_SCHEMA_KEYS = {
    "anyOf", "description", "enum", "example", "format", "items",
    "maxItems", "maxLength", "maximum", "minItems", "minLength", "minimum",
    "nullable", "pattern", "properties", "propertyOrdering", "required",
    "title", "type",
}

#: Fields `types.Schema` has and the **function-calling API** refuses.
#:
#: Kept as its own named set because the difference is not guessable and cost a
#: round to learn. `Schema` is the SDK's general schema type, used for structured
#: output as well; `FunctionDeclaration.parameters` is validated server-side
#: against something narrower. So a key can pass pydantic locally and come back
#:
#:     Unknown name "additional_properties" at
#:     'tools[0].function_declarations[0].parameters.properties[3].value'
#:
#: which is a 400 on every call that agent makes, not a warning.
#:
#: `defs`/`ref` are here for a different reason: nothing resolves a `$ref` on
#: this path, and a reference the model cannot follow is worse than the loose
#: schema left behind without it.
_GEMINI_NOT_IN_FUNCTION_SCHEMA = {
    "additionalProperties", "default", "minProperties", "maxProperties",
    "defs", "ref",
}

#: Bounds Gemini has no exclusive form of. Carried into the description rather
#: than dropped: the model cannot honour a bound it is never told about.
_GEMINI_EXCLUSIVE_BOUNDS = {
    "exclusiveMinimum": "greater than",
    "exclusiveMaximum": "less than",
}

#: The type names `types.Type` has, lowercased. Mirrors
#: `[m.value.lower() for m in types.Type]`, kept as a literal so cleaning a
#: schema does not have to import the SDK.
#:
#: Needed because `type` is the one whitelisted keyword whose *value* is also
#: constrained, and the two ways it goes wrong fail differently:
#:
#: * A name off this list — `"bool"`, `"int"`, `"any"` — is *accepted* by
#:   pydantic with only a `UserWarning`, then shipped as `{"type": "bool"}` and
#:   refused by the API. A warning nobody reads, then a 400 on every call.
#: * A JSON Schema type *union*, `{"type": ["boolean", "string"]}`, is refused
#:   locally, which takes the whole tool list down with it.
_GEMINI_TYPE_NAMES = {
    "type_unspecified", "string", "number", "integer",
    "boolean", "array", "object", "null",
}


def _gemini_normalise_type(cleaned: dict) -> None:
    """Force `cleaned["type"]` into the single name Gemini's `Schema` accepts.

    `Schema.type` is a one-valued enum. JSON Schema's is a name *or a list of
    names*, and MCP servers do use the list form: zod's `z.union([z.boolean(),
    z.string()])` serialises to `{"type": ["boolean", "string"]}`. The official
    sequential-thinking server declares three fields that way, and the whole
    tool list came back as

        3 validation errors for FunctionDeclaration
        parameters.properties.nextThoughtNeeded.type
          Input should be 'TYPE_UNSPECIFIED', 'STRING', ...

    — which every agent with that server attached gave as its answer, on every
    turn, because the tool list is declared up front and validated as a whole.

    A union becomes, in order of preference:

    * `null` dropped from the members and `nullable` set instead — `["string",
      "null"]` is how JSON Schema spells optional, and `nullable` says it
      exactly.
    * one member left → that name, plainly.
    * several left → `anyOf` branches, the same translation `oneOf` already
      gets. The node's own `type` goes, because the branches carry it.
    * nothing left (`["null"]`) → `null`, which `Type.NULL` accepts.

    An unrecognised name is dropped rather than passed through, leaving the
    caller's typeless-node fallback to supply one. A loose type beats a 400.
    """
    declared = cleaned.get("type")

    if isinstance(declared, str):
        if declared.lower() not in _GEMINI_TYPE_NAMES:
            cleaned.pop("type")
        return

    if not isinstance(declared, list):
        # A dict, a number, anything else: no field to land in.
        if declared is not None:
            cleaned.pop("type")
        return

    members = [t for t in declared if isinstance(t, str) and t.lower() in _GEMINI_TYPE_NAMES]
    nullable = any(t.lower() == "null" for t in members)
    members = [t for t in members if t.lower() != "null"]

    if nullable and members:
        cleaned["nullable"] = True

    if len(members) == 1:
        cleaned["type"] = members[0]
    elif members:
        # `anyOf` from a real `oneOf` already describes this node fully; a
        # second opinion on the type would only contradict it.
        if "anyOf" not in cleaned:
            cleaned["anyOf"] = [{"type": t} for t in members]
        cleaned.pop("type")
    elif nullable:
        cleaned["type"] = "null"
    else:
        cleaned.pop("type")


def _clean_schema_for_gemini(schema: dict) -> dict:
    """Rewrite a JSON schema into the subset Gemini's `Schema` accepts.

    ## Why this is more than a key filter

    It was one, and it worked until a real MCP server arrived. Vercel's
    `update_project_deployment_protection` declares a `passwordProtection`
    property as `oneOf`, and the whole call failed with

        3 validation errors for FunctionDeclaration
        parameters.properties.passwordProtection.oneOf
          Extra inputs are not permitted

    — which the customer saw as their agent's answer. One property on one tool
    of one server takes down every turn that agent makes, because the tool list
    is declared up front and validated as a whole.

    Simply adding `oneOf` to the drop list is worse than leaving it: what
    remains is a property with no `type`, and Gemini rejects *that* with a
    different error. The composition keywords have to be translated.

    ## The translation

    * `oneOf` → `anyOf`, which `Schema` does have. The distinction — exactly one
      branch versus at least one — is not something a model choosing a call
      shape acts on, and a slightly loose schema is worth far more than a tool
      the model cannot see at all.
    * `allOf` → merged into the parent. Members are almost always object
      fragments contributing properties, which is exactly what a merge produces.
    * `const` → a single-value `enum`, which is what `const` means.
    * `not` → dropped. There is no expressing it, and a schema that cannot say
      "not this" is still usable for choosing arguments.
    * A node left with no `type` gets one, because Gemini requires it: `object`
      when it has properties, `string` otherwise. `anyOf` nodes are exempt —
      they carry their type in their branches.
    """
    if not isinstance(schema, dict):
        return schema

    cleaned: dict = {}

    # `allOf` first: it contributes keys the rest of this node may override.
    for member in schema.get("allOf", []) or []:
        if isinstance(member, dict):
            merged = _clean_schema_for_gemini(member)
            for key, value in merged.items():
                if key == "properties" and isinstance(value, dict):
                    cleaned.setdefault("properties", {}).update(value)
                elif key == "required" and isinstance(value, list):
                    cleaned["required"] = [*cleaned.get("required", []), *value]
                else:
                    cleaned.setdefault(key, value)

    notes: list[str] = []
    for k, v in schema.items():
        if k in _GEMINI_EXCLUSIVE_BOUNDS:
            notes.append(f"{_GEMINI_EXCLUSIVE_BOUNDS[k]} {json.dumps(v)}")
            continue
        # `oneOf`, `allOf`, `const` and `not` are translated below or dropped;
        # everything else outside the whitelist has no field to land in.
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k in _SCHEMA_MAPS and isinstance(v, dict):
            # A map of *name → schema*, not a schema. Recursing into it as one
            # is how `properties` acquired a `type` of its own and Gemini
            # answered `properties.type: Input should be a valid dictionary`.
            cleaned[k] = {
                name: _clean_schema_for_gemini(sub) if isinstance(sub, dict) else sub
                for name, sub in v.items()
            }
        elif isinstance(v, dict):
            cleaned[k] = _clean_schema_for_gemini(v)
        elif isinstance(v, list):
            cleaned[k] = [_clean_schema_for_gemini(i) if isinstance(i, dict) else i for i in v]
        else:
            cleaned[k] = v

    branches = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(branches, list) and branches:
        cleaned["anyOf"] = [
            _clean_schema_for_gemini(b) for b in branches if isinstance(b, dict)
        ]

    if "const" in schema:
        cleaned.update(_gemini_fixed_value(schema["const"]))

    # Gemini's `enum` is `list[str]`. A numeric or boolean enum has to keep its
    # *type* instead — stringifying `true` to `"true"` would have the model send
    # a string where the tool wants a boolean, which is a worse failure than a
    # slightly looser schema because it happens at call time, inside the tool.
    values = cleaned.get("enum")
    if isinstance(values, list) and values and not all(isinstance(v, str) for v in values):
        cleaned.pop("enum")
        cleaned["type"] = _gemini_json_type(values[0])
        allowed = ", ".join(json.dumps(v) for v in values)
        cleaned["description"] = (
            f"{cleaned.get('description', '')} (one of: {allowed})".strip()
        )

    # After the `const` and numeric-`enum` branches above, which assign `type`
    # themselves, and before the fallback below, which is what a dropped name
    # falls back *to*.
    _gemini_normalise_type(cleaned)

    if "type" not in cleaned and "anyOf" not in cleaned:
        cleaned["type"] = "object" if "properties" in cleaned else "string"

    if notes:
        cleaned["description"] = (
            f"{cleaned.get('description', '')} (must be {', '.join(notes)})".strip()
        )

    # A merged `allOf` can repeat a required name.
    if isinstance(cleaned.get("required"), list):
        cleaned["required"] = list(dict.fromkeys(cleaned["required"]))

    return cleaned


def _convert_messages_for_gemini(messages: list[dict], images: list[str] | None = None):
    """Convert OpenAI-style messages to Gemini Content objects.

    Maps roles: 'user' → 'user', 'assistant' → 'model', 'system' → skip (handled separately).
    If images are provided, they are attached to the last user message.
    """
    from google.genai import types

    contents = []
    # Find the index of the last user message for image injection
    last_user_idx = -1
    filtered = [(msg, idx) for idx, msg in enumerate(messages) if msg.get("role") != "system" and msg.get("content")]
    if images:
        for i, (msg, _) in enumerate(filtered):
            if msg.get("role") == "user":
                last_user_idx = i

    for i, (msg, _) in enumerate(filtered):
        role = msg.get("role", "user")
        text = msg.get("content", "")
        gemini_role = "model" if role == "assistant" else "user"
        parts = [types.Part.from_text(text=text)]

        # Attach images to the last user message
        if images and i == last_user_idx:
            for img in images[:5]:
                mime, b64 = _parse_data_uri(img)
                parts.append(types.Part.from_bytes(
                    data=base64.b64decode(b64),
                    mime_type=mime,
                ))

        contents.append(types.Content(role=gemini_role, parts=parts))
    return contents


def _extract_gemini_response(response) -> tuple[str, str]:
    """Split a Gemini response into (text, reasoning).

    Gemini marks a thought part with `thought=True` — but only when the request
    asked for thoughts (`include_thoughts`), which is what `call_gemini` now
    does. When that flag is present the split is exact, and this is the whole
    reason Gemini's thinking used to read so badly:

    **Everything used to be a guess based on position.** Text alongside a
    function call was assumed to be reasoning and wrapped in `[REASONING]`;
    text on its own was assumed to be the answer. Both assumptions are wrong
    half the time. A model that explained itself before answering had the
    explanation shown as the answer, and a model that wrote its answer next to
    a tool call had the answer filed away as thinking and dropped.

    With no thought parts — an older model, or thoughts turned off — the
    fallback is the old positional rule, so nothing regresses. What it no
    longer does is *invent* `[REASONING]` markers: the reasoning comes back as
    its own return value, and `core.react_engine` puts it on the event.
    """
    if not response.candidates:
        return "Error: No response candidates from Gemini.", ""

    candidate = response.candidates[0]

    if candidate.finish_reason and candidate.finish_reason.name == "SAFETY":
        return "Error: Response blocked by Gemini safety filters.", ""

    if not candidate.content or not candidate.content.parts:
        reason = candidate.finish_reason.name if candidate.finish_reason else "UNKNOWN"
        return f"Error: Empty Gemini response. Finish Reason: {reason}", ""

    # Walk parts in order, keeping thoughts, answer text and calls apart.
    thought_parts: list[str] = []
    text_parts: list[str] = []
    function_calls: list[dict] = []
    for p in candidate.content.parts:
        if p.function_call:
            fc = p.function_call
            args = dict(fc.args) if fc.args else {}
            function_calls.append({"tool": fc.name, "arguments": args})
        elif p.text:
            # `getattr`, not `p.thought`: the attribute is absent on an older
            # SDK's Part and on anything shim-shaped, and an AttributeError
            # here is not a small failure — it is raised inside the retry loop,
            # so it costs five attempts with backoff before surfacing as
            # "Gemini is down".
            (thought_parts if getattr(p, "thought", None) else text_parts).append(p.text)

    reasoning = "\n".join(t for t in thought_parts if t and t.strip()).strip()
    answer = "\n".join(t for t in text_parts if t and t.strip()).strip()

    if function_calls:
        # Every call, in order; this used to keep the first and log the rest.
        call_json = "\n".join(json.dumps(fc) for fc in function_calls)
        if not reasoning and answer:
            # No thought parts came back, so fall back to the positional rule:
            # prose sitting beside a call is the model explaining the call.
            reasoning, answer = answer, ""
        return call_json, reasoning

    if answer:
        return answer, reasoning

    # Thoughts but no answer. The model spent its whole budget thinking, which
    # is a real outcome at a low cap — say so rather than returning nothing,
    # because an empty string reads to the loop as a finished final response.
    if reasoning:
        return "Error: Gemini returned only reasoning — raise the response cap or lower the reasoning effort.", reasoning

    return "Error: Gemini returned no usable content.", ""

# Global singleton Gemini client — reuses connection pool, prevents socket exhaustion
_gemini_client = None


async def call_gemini(model, messages, system, api_key, tools=None, images=None, prompt_cache: bool = True,
                     max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS, reasoning=None):
    """Call Gemini using the google-genai SDK with native function calling.

    Gemini implicit caching (Gemini 2.5 and newer) auto-applies on the server
    when prompts share a 1024+ token prefix. The hit count is reported in
    `usage_metadata.cached_content_token_count`. We do not (yet) use the
    explicit `cached_content` API since it requires lifecycle management.

    `max_tokens` arrives here for the first time. Every other provider was
    given the tenant's `max_output_tokens` when the four hardcoded 4096s were
    replaced; Gemini was skipped because it had no hardcoded cap to replace —
    which left it as the one provider still generating without a bound.

    `reasoning` is a `core.reasoning.ReasoningPlan` or None. Gemini changed
    knobs between generations: 2.5 takes a token budget, 3.x takes a named
    level. Sending the wrong one is not an error, it is silence, so the
    generation is chosen from the model name rather than tried and caught.
    """
    _ = prompt_cache  # reserved for future explicit cached_content support
    global _gemini_client
    from google import genai
    from google.genai import errors as _genai_errors
    from google.genai import types

    GEMINI_TIMEOUT = LLM_REQUEST_TIMEOUT   # seconds per attempt
    MAX_RETRIES = 5

    if _gemini_client is None:
        _gemini_client = genai.Client(
            api_key=api_key,
            # HTTP timeout 5s above wait_for so wait_for fires first for clean handling
            http_options=types.HttpOptions(timeout=int((GEMINI_TIMEOUT+5) * 1000)),  # seconds
        )

    contents = _convert_messages_for_gemini(messages, images=images)
    gemini_tools = _convert_tools_for_gemini(tools)

    def _build_config(with_tools: bool):
        """One builder for both configs.

        The MALFORMED_FUNCTION_CALL retry below used to construct a second
        `GenerateContentConfig` by hand, carrying only `system_instruction` —
        so the retry silently ran without whatever the first config had set.
        That was survivable when there was nothing else to set. With a response
        cap and a thinking budget on it, a retry that drops them is a different
        call answering a different question.
        """
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )
        if with_tools and gemini_tools:
            cfg.tools = gemini_tools
        if reasoning is not None:
            # Built in one constructor call rather than assigned field by
            # field: pydantic validates constructor arguments and does not
            # validate assignments, so `thinking_level = "HIHG"` would sail
            # through as a plain string and reach the wire unchecked.
            knob = ({"thinking_level": reasoning.level} if model.startswith("gemini-3")
                    else {"thinking_budget": reasoning.budget})
            cfg.thinking_config = types.ThinkingConfig(include_thoughts=True, **knob)
        return cfg

    config = _build_config(with_tools=True)

    async def _call(cfg, attempt_label=""):
        _log.debug(f"🔄 Gemini _call start ({attempt_label})")
        result = await asyncio.to_thread(
            _gemini_client.models.generate_content,
            model=model,
            contents=contents,
            config=cfg,
        )
        _log.debug(f"✅ Gemini _call complete ({attempt_label})")
        return result

    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]  # seconds between retries
    last_error = None
    refused = False
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            response = await _call(config, f"attempt {attempt}")
            result, reasoning_text = _extract_gemini_response(response)

            # If MALFORMED_FUNCTION_CALL, retry once without tools (forces text response)
            if "MALFORMED_FUNCTION_CALL" in result and gemini_tools:
                _log.debug("Gemini MALFORMED_FUNCTION_CALL — retrying without tools")
                response = await _call(_build_config(with_tools=False), "no-tools retry")
                result, reasoning_text = _extract_gemini_response(response)

            # Extract actual token usage from Gemini response metadata
            input_tokens = 0
            output_tokens = 0
            cache_read = 0
            cache_write = 0
            try:
                um = getattr(response, 'usage_metadata', None)
                if um:
                    input_tokens = getattr(um, 'prompt_token_count', 0) or 0
                    output_tokens = getattr(um, 'candidates_token_count', 0) or 0
                    # Thinking is billed as output and is reported separately —
                    # so leaving it out did not merely under-report the count,
                    # it under-charged the call. At `high` that is most of it.
                    output_tokens += int(getattr(um, 'thoughts_token_count', 0) or 0)
                    cache_read = int(getattr(um, 'cached_content_token_count', 0) or 0)
                    # Gemini's prompt_token_count is INCLUSIVE of cached tokens.
                    if cache_read:
                        input_tokens = max(0, input_tokens - cache_read)
            except Exception:
                pass
            _finish = None
            try:
                _finish = response.candidates[0].finish_reason
            except (AttributeError, IndexError, TypeError):
                pass
            return LLMResult(result, input_tokens, output_tokens, cache_read, cache_write,
                             _normalise_stop_reason(_finish), (), reasoning_text)

        # except asyncio.TimeoutError:
        #     last_error = f"Request timed out ({GEMINI_TIMEOUT}s)"
        #     print(f"DEBUG: ⚠️ Gemini timeout on attempt {attempt}/{MAX_RETRIES}. Retrying in {backoff}s...")
        #     if attempt < MAX_RETRIES:
        #         await asyncio.sleep(backoff)
        #         continue
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ Unexpected {type(e).__name__}: {vars(e) if hasattr(e, '__dict__') else e}")
            _log.warning(f"⚠️ Gemini error on attempt {attempt}/{MAX_RETRIES}: {e}")
            # A request Gemini refuses (400 malformed, 401/403 key, 404 model) is
            # refused the same way every time; retrying it cost 79s of backoff
            # before the person saw the first answer. Same rule as the other
            # providers: only rate limits, cancellations and server errors retry.
            # Anything that is not an API answer at all (a dropped connection)
            # still does.
            code = getattr(e, "code", None) if isinstance(e, _genai_errors.APIError) else None
            if isinstance(code, int) and code not in (408, 429, 499) and code < 500:
                refused = True
                break
            if attempt < MAX_RETRIES:
                _retry_notice("Gemini", f"failed ({type(e).__name__})", attempt, MAX_RETRIES, backoff)
                await asyncio.sleep(backoff)
                continue

    error_msg = (
        f"Gemini LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
        if not refused
        else f"Gemini LLM Error: the request was refused. {last_error}"
    )
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)

async def call_grok(model, messages, system, api_key, tools=None, images=None, prompt_cache: bool = True,
                    max_tokens: int | None = None, reasoning=None):
    """Call xAI Grok via its OpenAI-compatible API with 5-attempt exponential backoff.

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    """
    GROK_TIMEOUT = LLM_REQUEST_TIMEOUT
    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]

    # Build full messages list with system prompt
    full_messages = []
    if system and str(system).strip():
        full_messages.append({"role": "system", "content": str(system).strip()})
    full_messages.extend(messages or [])

    # Inject images into the last user message (Grok is OpenAI-compatible)
    if images:
        for i in range(len(full_messages) - 1, -1, -1):
            if full_messages[i].get("role") == "user":
                full_messages[i] = dict(full_messages[i])
                full_messages[i]["content"] = _build_openai_image_content(
                    str(full_messages[i].get("content", "")), images
                )
                break

    payload: dict = {"model": model, "messages": full_messages,
                     "max_tokens": max_tokens or DEFAULT_MAX_OUTPUT_TOKENS}
    _apply_openai_reasoning(payload, reasoning)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            _log.debug(f"🔄 Grok call start (attempt {attempt}/{MAX_RETRIES})")
            async with httpx.AsyncClient(timeout=GROK_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            if resp.status_code in (429, 499, 500, 502, 503, 529):
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                _retry_notice("Grok", f"answered {resp.status_code}", attempt, MAX_RETRIES, backoff)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    continue
            if resp.status_code == 400 and _retry_without_reasoning(payload, resp):
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            from core.cache.prompt_cache import extract_openai_cache_tokens
            cache_read, cache_write = extract_openai_cache_tokens(usage) if prompt_cache else (0, 0)
            if cache_read:
                input_tokens = max(0, input_tokens - cache_read)
            choice = data["choices"][0]
            msg = choice.get("message", {})
            text, _reasoning = _openai_compat_extract(msg)
            _log.debug(f"✅ Grok call complete (attempt {attempt})")
            return LLMResult(text, input_tokens, output_tokens, cache_read, cache_write,
                             _normalise_stop_reason(choice.get("finish_reason")), (), _reasoning)
        except httpx.TimeoutException:
            last_error = f"Request timed out ({GROK_TIMEOUT}s)"
            _retry_notice("Grok", f"did not answer within {GROK_TIMEOUT:g}s", attempt, MAX_RETRIES, backoff)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {str(e)[:200]}"
            _log.warning(f"❌ Grok HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES and e.response.status_code in (429, 499, 500, 502, 503, 529):
                await asyncio.sleep(backoff)
                continue
            break
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ Grok error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue

    error_msg = f"Grok LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)


async def call_deepseek(model, messages, system, api_key, tools=None, images=None, prompt_cache: bool = True,
                        max_tokens: int | None = None, reasoning=None):
    """Call DeepSeek via its OpenAI-compatible API with 5-attempt exponential backoff.

    DeepSeek prompt caching is automatic server-side. It surfaces hit/miss as
    `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` in `usage`. We just
    read those and report them.

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    """
    # DeepSeek does not support vision — drop images with a warning
    if images:
        _log.warning(f"⚠️ DeepSeek does not support images. {len(images)} image(s) will be dropped.")
    DEEPSEEK_TIMEOUT = LLM_REQUEST_TIMEOUT
    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]

    # Build full messages list with system prompt
    full_messages = []
    if system and str(system).strip():
        full_messages.append({"role": "system", "content": str(system).strip()})
    full_messages.extend(messages or [])

    payload: dict = {"model": model, "messages": full_messages,
                     "max_tokens": max_tokens or DEFAULT_MAX_OUTPUT_TOKENS}
    _apply_openai_reasoning(payload, reasoning)
    # DeepSeek-Reasoner does not support function calling
    if tools and "reasoner" not in model.lower():
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            _log.debug(f"🔄 DeepSeek call start (attempt {attempt}/{MAX_RETRIES})")
            async with httpx.AsyncClient(timeout=DEEPSEEK_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            if resp.status_code in (429, 499, 500, 502, 503, 529):
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                _retry_notice("DeepSeek", f"answered {resp.status_code}", attempt, MAX_RETRIES, backoff)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    continue
            if resp.status_code == 400 and _retry_without_reasoning(payload, resp):
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            from core.cache.prompt_cache import extract_deepseek_cache_tokens
            cache_read, cache_write = extract_deepseek_cache_tokens(usage) if prompt_cache else (0, 0)
            # DeepSeek's prompt_tokens already separates hit/miss via prompt_cache_*_tokens,
            # so input_tokens here is the MISS count already; do NOT subtract again.
            choice = data["choices"][0]
            msg = choice.get("message", {})
            text, _reasoning = _openai_compat_extract(msg)
            _log.debug(f"✅ DeepSeek call complete (attempt {attempt})")
            return LLMResult(text, input_tokens, output_tokens, cache_read, cache_write,
                             _normalise_stop_reason(choice.get("finish_reason")), (), _reasoning)
        except httpx.TimeoutException:
            last_error = f"Request timed out ({DEEPSEEK_TIMEOUT}s)"
            _retry_notice("DeepSeek", f"did not answer within {DEEPSEEK_TIMEOUT:g}s", attempt, MAX_RETRIES, backoff)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {str(e)[:200]}"
            _log.warning(f"❌ DeepSeek HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES and e.response.status_code in (429, 499, 500, 502, 503, 529):
                await asyncio.sleep(backoff)
                continue
            break
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ DeepSeek error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue

    error_msg = f"DeepSeek LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)


def _normalize_v1_base_url(base_url: str) -> str:
    """Normalize a v1-compatible base URL by stripping trailing /v1."""
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


async def call_v1_compatible(model, messages, system, base_url, api_key, tools=None, images=None, prompt_cache: bool = True,
                             reasoning=None):
    """Call any OpenAI v1-compatible endpoint with 5-attempt exponential backoff.

    Used for both cloud OpenAI-compatible providers (OpenRouter, Together, etc.)
    and local v1-compatible servers (vLLM, LM Studio, etc.).

    The model name is expected to have its prefix (oaic. or locv1.) already stripped
    before calling this function.

    Args:
        model: Model name as known by the remote API (prefix already stripped)
        messages: List of {"role": ..., "content": ...} dicts
        system: System instruction text
        base_url: Base URL (e.g. https://openrouter.ai/api) — /v1/chat/completions appended
        api_key: API key (may be empty for local servers with no auth)
        tools: Ollama-format tool list (forwarded as OpenAI function definitions)
        images: List of base64 data-URI image strings to attach to the last user message

    Returns:
        (response_text, input_tokens, output_tokens)
    """
    if not base_url or not base_url.strip():
        raise LLMError("V1-compatible provider: base URL is not configured.")

    V1_TIMEOUT = LLM_REQUEST_TIMEOUT
    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]

    full_messages = []
    if system and str(system).strip():
        full_messages.append({"role": "system", "content": str(system).strip()})
    full_messages.extend(messages or [])

    if images:
        for i in range(len(full_messages) - 1, -1, -1):
            if full_messages[i].get("role") == "user":
                full_messages[i] = dict(full_messages[i])
                full_messages[i]["content"] = _build_openai_image_content(
                    str(full_messages[i].get("content", "")), images
                )
                break

    payload: dict = {"model": model, "messages": full_messages}
    _apply_openai_reasoning(payload, reasoning)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    endpoint = f"{_normalize_v1_base_url(base_url)}/v1/chat/completions"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            async with httpx.AsyncClient(timeout=V1_TIMEOUT) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code in (429, 499, 500, 502, 503, 529):
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                _retry_notice("V1-compatible", f"answered {resp.status_code}", attempt, MAX_RETRIES, backoff)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)
                    continue
            if resp.status_code == 400 and _retry_without_reasoning(payload, resp):
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            from core.cache.prompt_cache import extract_openai_cache_tokens
            cache_read, cache_write = extract_openai_cache_tokens(usage) if prompt_cache else (0, 0)
            if cache_read:
                input_tokens = max(0, input_tokens - cache_read)
            choice = data["choices"][0]
            msg = choice.get("message", {})
            text, _reasoning = _openai_compat_extract(msg)
            _log.debug(f"✅ V1-compatible call complete (attempt {attempt})")
            return LLMResult(text, input_tokens, output_tokens, cache_read, cache_write,
                             _normalise_stop_reason(choice.get("finish_reason")), (), _reasoning)
        except httpx.TimeoutException:
            last_error = f"Request timed out ({V1_TIMEOUT}s)"
            _retry_notice("V1-compatible", f"did not answer within {V1_TIMEOUT:g}s", attempt, MAX_RETRIES, backoff)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue
        except httpx.HTTPStatusError as e:
            # The body, not str(e): a gateway's refusal carries its reason (and
            # figures) in the response body, and the URL alone explains nothing.
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            _log.warning(f"❌ V1-compatible HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES and e.response.status_code in (429, 499, 500, 502, 503, 529):
                await asyncio.sleep(backoff)
                continue
            break
        except Exception as e:
            last_error = str(e)
            _log.warning(f"⚠️ V1-compatible error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff)
                continue

    error_msg = f"V1-compatible LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)


async def call_model_gateway(model, messages, system, settings, tools=None, images=None, prompt_cache: bool = True,
                             reasoning=None):
    """Call the configured model gateway (synapse.* models).

    A first-class provider like `call_openai`, except the endpoint belongs to
    the deployment rather than to a vendor: `model_gateway_base_url` and
    `model_gateway_key` name a v1-compatible service the operator runs —
    LiteLLM, a proxy, or a hosted platform's own model service.

    Delegates the HTTP work to `call_v1_compatible` — the gateway speaks the
    OpenAI v1 shape precisely so a second client does not exist to drift —
    and adds the one behaviour the generic caller cannot: a 402 (the gateway
    refusing over quota or balance) is re-raised carrying the gateway's own
    sentence, which is where the figures are. A refusal with no number in it
    is unanswerable from a run log.
    """
    real_model = model[len("synapse."):] if model.startswith("synapse.") else model
    try:
        return await call_v1_compatible(
            real_model,
            messages,
            system,
            settings.get("model_gateway_base_url", ""),
            settings.get("model_gateway_key", ""),
            tools=tools,
            images=images,
            prompt_cache=prompt_cache,
            reasoning=reasoning,
        )
    except LLMError as e:
        if "HTTP 402" not in str(e):
            raise
        detail = re.search(r'"message"\s*:\s*"([^"]+)"', str(e))
        if detail is None:
            raise
        raise LLMError(f"Model gateway refused the call: {detail.group(1)}") from e


async def _bedrock_converse_direct(
    region: str, api_key: str, model_id: str,
    messages: list, system_blocks: list, max_tokens: int,
    additional: dict | None = None,
) -> dict:
    """Call Bedrock Converse via boto3 with UNSIGNED auth + before-send bearer injection.

    Used for long-term ABSK keys. boto3's before-send event fires right before urllib3
    sends the HTTP request; we inject Authorization: Bearer {api_key} so botocore signing
    (disabled via UNSIGNED) cannot interfere.
    boto3 handles image-bytes serialisation natively; no manual base64 conversion needed.
    """
    def _make_client():
        session = boto3.session.Session()

        def _inject_auth(request, **kwargs):  # noqa: ARG001
            request.headers['Authorization'] = f'Bearer {api_key}'

        # Register on the session so it applies to this client only.
        # before-send fires with AWSPreparedRequest; returning None lets the
        # call proceed normally with the injected header.
        session.events.register(
            'before-send.bedrock-runtime.Converse',
            _inject_auth,
        )

        return session.client(
            'bedrock-runtime',
            region_name=region,
            aws_access_key_id='unsigned',
            aws_secret_access_key='unsigned',
            config=Config(signature_version=_BEDROCK_UNSIGNED),
        )

    def _run():
        client = _make_client()
        try:
            return client.converse(
                modelId=model_id,
                messages=messages,
                system=system_blocks,
                inferenceConfig={'maxTokens': max_tokens},
                **({'additionalModelRequestFields': additional} if additional else {}),
            )
        except botocore.exceptions.ClientError as e:
            # Re-raised as it came: the error code is what `call_bedrock` decides
            # on (F6). This used to become `RuntimeError(f"HTTP {status}: …")`,
            # which no rule could tell apart.
            raise

    return await asyncio.to_thread(_run)


_THINK_TAG_RE = re.compile(r"<(think(?:ing)?)>(.*?)</\1>\s*", re.DOTALL | re.IGNORECASE)


def _split_think_tags(text: str) -> tuple[str, str]:
    """Pull `<think>...</think>` and `<thinking>...</thinking>` out of a reply
    into (text, reasoning).

    Local reasoning models — qwen3, deepseek-r1, anything served raw by Ollama
    or llama.cpp — write their thinking inline in `<think>`; Amazon Nova writes
    it in `<thinking>`. Nothing parsed them, so the tags and everything in them
    were shown to the person as the agent's answer. Ollama's own `thinking`
    field is preferred where the server is new enough to send it; this is the
    fallback for where it is not.

    `generate_response_ex` applies it once, after every provider, not per branch
    (F32): Nova reaches the engine through the model gateway and through an
    own-key Bedrock, and the per-branch version covered neither.
    """
    if not text or "<think" not in text.lower():
        return text, ""
    thoughts = [m[1].strip() for m in _THINK_TAG_RE.findall(text) if m[1].strip()]
    return _THINK_TAG_RE.sub("", text).strip(), "\n".join(thoughts).strip()


def _extract_bedrock_content(message: dict) -> tuple[str, str]:
    """Split a Converse output message into (text, reasoning).

    This used to be `content[0].get("text", "")` — the first block, assumed to
    be the answer. With reasoning enabled that assumption inverts: the first
    block is `reasoningContent` and the answer is the second, so taking the
    first would have returned the thinking as the reply and dropped the reply.
    Walking the list costs nothing and is correct either way.
    """
    blocks = message.get("content") or []
    if not isinstance(blocks, list):
        return "", ""
    text_parts, thought_parts = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("text"):
            text_parts.append(block["text"])
        elif "reasoningContent" in block:
            # `reasoningText.text` is the prose; a `redactedContent` sibling is
            # an encrypted blob with nothing to show a person.
            thought = ((block.get("reasoningContent") or {}).get("reasoningText") or {}).get("text")
            if thought:
                thought_parts.append(thought)
    return "\n".join(text_parts).strip(), "\n".join(thought_parts).strip()


#: Bedrock errors worth another attempt — the model gateway's own set
#: (`api/gateway/bedrock.py`), so the two paths to the same service agree.
#: Decided by **error code**, never by HTTP status: throttling and quota errors
#: do not share one status, and a 400 is as often a refusal as a quota.
_BEDROCK_RETRYABLE_CODES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
    "ModelNotReadyException",
    "ServiceUnavailableException",
    "InternalServerException",
})


def _bedrock_error(exc: BaseException) -> tuple[str, str]:
    """(code, message) of a Bedrock client error, or ("", "") for anything else
    — a dropped connection, a timeout — which stays retryable."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", ""
    error = response.get("Error") or {}
    return str(error.get("Code") or ""), str(error.get("Message") or "")


def _converse_unsupported(code: str, message: str) -> bool:
    """Bedrock's answer for a model with no Converse API — the one case the
    InvokeModel fallback exists for. The wording is Bedrock's and not a contract,
    so it is matched loosely."""
    return code == "ValidationException" and "support" in message.lower()


def _bedrock_needs_profile(model_id: str, settings: dict) -> str:
    """The on-demand refusal, saying what to pick — and naming a workspace's stored profile.

    `bedrock_inference_profile` stopped being applied (F66). A workspace that
    relied on it gets this error on its next call, so the message names the
    profile it had stored as the model to pick instead.
    """
    message = (
        f"Bedrock model '{model_id}' has no on-demand throughput: pick an inference profile as the "
        "model instead (the us./eu./apac./global. variant, or an application profile's ARN)."
    )
    stored = (settings.get("bedrock_inference_profile") or "").strip().removeprefix("bedrock.")
    if stored:
        message += (
            f" This workspace has the inference profile '{stored}' stored, which is no longer applied "
            f"to every Bedrock model — bedrock.{stored} is the model to pick."
        )
    return message


async def call_bedrock(model_id, messages, system, region, settings, images=None, prompt_cache: bool = True,
                       max_tokens: int | None = None, reasoning=None):
    """Call AWS Bedrock with 5-attempt exponential backoff retry loop.

    Prefers the Converse API (standardized); falls back to InvokeModel only for a
    model Bedrock says has no Converse API. Retries only what
    `_BEDROCK_RETRYABLE_CODES` names, plus errors with no code at all (a dropped
    connection); anything Bedrock refused raises `LLMError` at once, naming the
    code (F6).

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        — token counts are zero for the InvokeModel fallback path (older models).
    """
    MAX_RETRIES = 5
    BACKOFF_SCHEDULE = [5, 10, 20, 40, 80]

    # Bedrock requires the exact model ID (e.g., anthropic.claude-3-5-sonnet-20240620-v1:0)
    # We strip the 'bedrock.' prefix if present
    real_model_id = model_id.replace("bedrock.", "")

    # The model picked is the model called. An inference profile is picked as
    # a model (`bedrock.us.anthropic.…`, or an application profile's ARN) —
    # discovery lists them. The workspace `bedrock_inference_profile` used to
    # replace every Bedrock model with itself, whatever the agent picked, and
    # with the picked model's reasoning payload (F66). It is no longer applied;
    # a stored value is only named in the on-demand refusal below.
    invocation_model_id = real_model_id

    bedrock = _make_aws_client("bedrock-runtime", region, settings)
    bedrock_api_key = _normalize_bedrock_api_key(settings)
    effective_region = (region or settings.get("aws_region") or "us-east-1").strip()
    # `bedrock_max_tokens` stays the explicit per-tenant override; what changed
    # is the floor under it. Bedrock kept a 4096 default of its own when the
    # other providers moved to `max_output_tokens`, so a Bedrock model was
    # still truncating at half the cap every other provider had.
    max_tokens = int(settings.get("bedrock_max_tokens") or max_tokens or DEFAULT_MAX_OUTPUT_TOKENS)

    # Reasoning travels outside the Converse schema, in the passthrough field
    # each model family defines for itself.
    _additional: dict = {}
    if reasoning is not None:
        max_tokens = max(max_tokens, reasoning.max_output_tokens)
        _additional["reasoning_config"] = {
            "type": "enabled",
            "budget_tokens": reasoning.budget,
        }

    # Normalize messages to Bedrock Converse content-block format
    normalized_messages = []
    # Find the last user message index for image injection
    all_msgs = [m for m in (messages or []) if m.get("role") in ("user", "assistant")]
    last_user_idx = -1
    if images:
        for idx_m, m in enumerate(all_msgs):
            if m.get("role") == "user":
                last_user_idx = idx_m

    for idx_m, m in enumerate(all_msgs):
        role = m.get("role")
        content = m.get("content")
        blocks = []
        if isinstance(content, str):
            blocks.append({"text": content})
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and "text" in b:
                    blocks.append({"text": str(b.get("text"))})
                elif isinstance(b, dict) and b.get("type") == "text" and "text" in b:
                    blocks.append({"text": str(b.get("text"))})
                else:
                    blocks.append({"text": str(b)})
        else:
            blocks.append({"text": str(content)})

        # Inject images into the last user message
        if images and idx_m == last_user_idx:
            for img in images[:5]:
                mime, b64 = _parse_data_uri(img)
                # Map MIME to Bedrock format name
                # No `.get(mime, "png")` fallback any more. It labelled a
                # HEIC as a PNG and let Bedrock discover the lie; the mime is
                # now one of four by the time it reaches here.
                fmt = {"image/png": "png", "image/jpeg": "jpeg",
                       "image/gif": "gif", "image/webp": "webp"}[mime]
                blocks.append({"image": {"format": fmt, "source": {"bytes": base64.b64decode(b64)}}})

        normalized_messages.append({"role": role, "content": blocks})

    system_blocks = []
    if system and str(system).strip():
        system_blocks = [{"text": str(system)}]
    if prompt_cache:
        from core.cache.prompt_cache import decorate_bedrock_system_blocks
        system_blocks = decorate_bedrock_system_blocks(system_blocks, str(system) if system else None)

    async def _converse_call():
        def _run():
            return bedrock.converse(
                modelId=invocation_model_id,
                messages=normalized_messages,
                system=system_blocks,
                inferenceConfig={"maxTokens": max_tokens},
                **({"additionalModelRequestFields": _additional} if _additional else {}),
            )
        return await asyncio.to_thread(_run)

    async def _invoke_model_call():
        anthropic_messages = []
        for m in normalized_messages:
            anthropic_messages.append({
                "role": m["role"],
                "content": [{"type": "text", "text": b.get("text", "")} for b in (m.get("content") or [])],
            })
        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": str(system or ""),
            "messages": anthropic_messages,
        }
        def _run():
            return bedrock.invoke_model(
                body=json.dumps(payload).encode("utf-8"),
                modelId=invocation_model_id,
                accept="application/json",
                contentType="application/json",
            )
        return await asyncio.to_thread(_run)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        backoff = BACKOFF_SCHEDULE[attempt - 1]
        try:
            _log.debug(f"🔄 Bedrock call start (attempt {attempt}/{MAX_RETRIES})")
            if hasattr(bedrock, "converse") or bedrock_api_key:
                try:
                    if bedrock_api_key:
                        resp = await _bedrock_converse_direct(
                            effective_region, bedrock_api_key, invocation_model_id,
                            normalized_messages, system_blocks, max_tokens,
                            _additional or None,
                        )
                    else:
                        resp = await _converse_call()
                    msg = (((resp or {}).get("output") or {}).get("message") or {})
                    text, _reasoning = _extract_bedrock_content(msg)
                    usage = (resp or {}).get("usage") or {}
                    inp = int(usage.get("inputTokens") or 0)
                    out = int(usage.get("outputTokens") or 0)
                    from core.cache.prompt_cache import extract_bedrock_cache_tokens
                    cache_read, cache_write = extract_bedrock_cache_tokens(resp or {})
                    # Bedrock inputTokens may include cache hits; subtract for consistent billing math.
                    if cache_read:
                        inp = max(0, inp - cache_read)
                    _log.debug(f"✅ Bedrock (converse) complete (attempt {attempt})")
                    return LLMResult(text, inp, out, cache_read, cache_write,
                                     _normalise_stop_reason((resp or {}).get("stopReason")),
                                     (), _reasoning)
                except Exception as converse_err:
                    msg_str = str(converse_err)
                    code, message = _bedrock_error(converse_err)
                    # Before `_converse_unsupported`: this refusal's text says
                    # "supported", which would send it to the InvokeModel fallback.
                    if "on-demand throughput" in msg_str:
                        raise LLMError(_bedrock_needs_profile(real_model_id, settings))
                    # Auth failures: raise immediately — InvokeModel also requires SigV4
                    # and won't help when bearer token auth fails.
                    if "AccessDeniedException" in msg_str or "HTTP 403" in msg_str or "Invalid API Key" in msg_str:
                        raise LLMError(
                            f"Bedrock authentication failed: {converse_err}. "
                            "Check your API key or IAM credentials in Settings."
                        )
                    if not _converse_unsupported(code, message):
                        # Retryable or refused, the outer handler decides — but
                        # never by trying an Anthropic-shaped InvokeModel payload
                        # against a model that has Converse. That turned a
                        # throttle into a second request and replaced the real
                        # error with the fallback's.
                        raise
                    _log.debug(f"Bedrock model has no Converse API (attempt {attempt}), using invoke_model: {converse_err}")
                    resp = await _invoke_model_call()
                    response_body = json.loads(resp.get("body").read()) if resp and resp.get("body") else {}
                    content = response_body.get("content") or []
                    text = content[0].get("text", "") if (content and isinstance(content, list) and isinstance(content[0], dict)) else ""
                    _log.debug(f"✅ Bedrock (invoke_model) complete (attempt {attempt})")
                    return LLMResult(text)
            else:
                resp = await _invoke_model_call()
                response_body = json.loads(resp.get("body").read()) if resp and resp.get("body") else {}
                content = response_body.get("content") or []
                text = content[0].get("text", "") if (content and isinstance(content, list) and isinstance(content[0], dict)) else ""
                _log.debug(f"✅ Bedrock (invoke_model) complete (attempt {attempt})")
                return LLMResult(text)
        except LLMError:
            raise  # Non-retryable (config error)
        except Exception as e:
            code, message = _bedrock_error(e)
            if code and code not in _BEDROCK_RETRYABLE_CODES:
                raise LLMError(f"Bedrock refused the request ({code}): {message or e}") from e
            last_error = str(e)
            _log.warning(f"⚠️ Bedrock error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                _log.debug(f"⏳ Retrying Bedrock in {backoff}s...")
                await asyncio.sleep(backoff)
                continue

    error_msg = f"Bedrock LLM Error: All {MAX_RETRIES} attempts failed. Last error: {last_error}"
    _log.warning(f"❌ {error_msg}")
    raise LLMError(error_msg)


# ─── HuggingFace local-model provider ───────────────────────────────────────────
#
# Loads transformers models in-process and runs inference on the host's GPU/CPU.
# Used for `hf.<org>/<model>` model names (e.g. `hf.Qwen/Qwen2.5-7B-Instruct`).
# Models are kept warm in a process-level cache so the 20-60s load cost is paid
# once per model, not per call. torch + transformers are lazy-imported so users
# who never use hf.* models don't need them installed.

_hf_model_cache: dict[str, tuple] = {}
_hf_cache_lock = threading.Lock()


def _hf_device() -> str:
    """Best available device for HF models: cuda > mps > cpu."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _load_hf_model(model_id: str, hf_token: str | None = None):
    """Load (or fetch from cache) an HF tokenizer + model pair.

    Lock-guarded so concurrent calls don't double-load the same model.
    """
    with _hf_cache_lock:
        if model_id in _hf_model_cache:
            return _hf_model_cache[model_id]

        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM
        except ImportError as e:
            raise LLMError(
                "HuggingFace provider: 'torch' and 'transformers' are not installed. "
                "Install them on the Synapse host (e.g. `pip install torch transformers`) "
                "to use hf.* models."
            ) from e

        device = _hf_device()
        dtype = torch.float16 if device in ("cuda", "mps") else torch.float32
        load_kwargs: dict = {"torch_dtype": dtype}
        if device == "cuda":
            load_kwargs["device_map"] = "auto"
        if hf_token:
            load_kwargs["token"] = hf_token

        _log.debug(f"🤗 Loading HF model '{model_id}' on {device} — first call pays 20-60s load cost...")

        tok_kwargs: dict = {"token": hf_token} if hf_token else {}
        tokenizer = AutoTokenizer.from_pretrained(model_id, **tok_kwargs)
        model = AutoModelForCausalLM.from_pretrained(model_id, **load_kwargs)

        # device_map="auto" already places shards; only move when we didn't use it.
        if device != "cuda":
            model = model.to(device)
        model.eval()

        _hf_model_cache[model_id] = (tokenizer, model)
        _log.debug(f"✅ HF model '{model_id}' loaded and cached.")
        return tokenizer, model


async def call_huggingface(
    model: str,
    messages: list[dict],
    system: str,
    settings: dict,
    tools: list[dict] | None = None,
    images: list[str] | None = None,
):
    """Run inference on a local HuggingFace transformers model.

    Args:
        model: Full model name with hf. prefix (e.g. 'hf.Qwen/Qwen2.5-7B-Instruct').
        messages: OpenAI-style [{"role": ..., "content": ...}] list.
        system: System instruction text.
        settings: User settings dict. Reads `huggingface_token` (for gated models)
                  and `huggingface_max_new_tokens` (default 1024).
        tools: Ignored with a warning — native tool calling varies by model and
               is left to the caller to inject via prompt text if needed.
        images: Ignored — vision is model-specific and not supported in this MVP.

    Returns:
        (response_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        — cache fields are always 0 (no cross-call prompt caching for local inference).
    """
    model_id = model[len("hf."):] if model.startswith("hf.") else model
    if not model_id:
        raise LLMError("HuggingFace provider: empty model name after 'hf.' prefix.")

    if tools:
        _log.warning(
            f"⚠️ HF provider received {len(tools)} tool(s) but does not support "
            f"native tool calling. Inject tool descriptions into the system prompt if needed.",
        )
    if images:
        _log.warning(
            f"⚠️ HF provider received {len(images)} image(s) but vision is not "
            f"supported in this MVP — they will be dropped.",
        )

    hf_token = (settings.get("huggingface_token") or "").strip() or None
    max_new_tokens = int(settings.get("huggingface_max_new_tokens") or 1024)

    def _run():
        import torch
        tokenizer, model_obj = _load_hf_model(model_id, hf_token)

        chat_messages: list[dict] = []
        if system and system.strip():
            chat_messages.append({"role": "system", "content": system.strip()})
        chat_messages.extend(messages or [])

        # Prefer the model's chat template; fall back to a plain transcript if
        # the tokenizer doesn't define one (some base / non-instruct models).
        try:
            input_ids = tokenizer.apply_chat_template(
                chat_messages,
                add_generation_prompt=True,
                return_tensors="pt",
            )
        except Exception as e:
            _log.warning(f"⚠️ HF chat template unavailable ({e}); using transcript fallback.")
            transcript_parts: list[str] = []
            if system and system.strip():
                transcript_parts.append(f"System: {system.strip()}")
            t = _messages_to_transcript(messages)
            if t:
                transcript_parts.append(t)
            transcript_parts.append("Assistant:")
            transcript = "\n\n".join(transcript_parts)
            input_ids = tokenizer(transcript, return_tensors="pt").input_ids

        input_ids = input_ids.to(model_obj.device)
        prompt_token_count = int(input_ids.shape[-1])

        with torch.no_grad():
            output_ids = model_obj.generate(
                input_ids,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )

        new_tokens = output_ids[0, prompt_token_count:]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        output_token_count = int(new_tokens.shape[-1])
        return text, prompt_token_count, output_token_count

    try:
        _log.debug(f"🔄 HF call start (model={model_id})")
        text, input_tokens, output_tokens = await asyncio.to_thread(_run)
        _log.debug(f"✅ HF call complete (model={model_id}, out_tokens={output_tokens})")
        return LLMResult(text, input_tokens, output_tokens)
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"HuggingFace provider error ({model_id}): {e}")


def _messages_to_transcript(messages: list[dict] | None) -> str:
    """Lossy conversion of role/content messages to plain text for providers that only accept a single prompt."""
    if not messages:
        return ""
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        content = m.get("content")
        if isinstance(content, list):
            # Best-effort concatenate any text blocks
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            text = "\n".join(parts).strip()
        else:
            text = (content or "").strip() if isinstance(content, str) else ""

        if not text:
            continue

        if role == "user":
            label = "User"
        elif role == "assistant":
            label = "Assistant"
        elif role:
            label = role.title()
        else:
            label = "Message"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


async def generate_response_ex(
    prompt_msg,
    sys_prompt,
    mode,
    current_model,
    current_settings,
    tools=None,
    history_messages=None,
    memory_context_text: str = "",
    # Usage tracking context — passed by react_engine & orchestration steps
    session_id: str | None = None,
    agent_id: str | None = None,
    source: str = "chat",
    run_id: str | None = None,
    tool_name: str | None = None,
    images: list[str] | None = None,
    # Response cache — opt-in per call; should NEVER be used for AGENT-step ReAct
    # loops because skipping the LLM call would diverge shared_state silently.
    cache_response: bool = False,
    cache_response_semantic: bool = False,
    cache_response_ttl: int = 3600,
    cache_response_step_id: str | None = None,
    cache_response_threshold: float = 0.95,
    # Prompt-cache layout — see core.cache.prompt_cache. Additive: `prompt_msg`
    # remains the full text every provider, the char counter and the response
    # cache see; these are the same text structured for a provider that can
    # mark a breakpoint inside the conversation. Only Anthropic reads them.
    prompt_blocks: list[str] | None = None,
    volatile_tail: str | None = None,
    # Reasoning effort — "off" | "low" | "medium" | "high", or None to take the
    # workspace default. Resolved against `core.reasoning`, which decides
    # whether this model has a native channel at all; a model that has none
    # keeps the `[REASONING]` prompt convention and ignores this entirely.
    reasoning_effort: str | None = None,
):
    """
    Unified LLM dispatch. Routes to the provider for `mode`/`current_model`,
    logs usage, and returns the full `LLMResult`.

    `generate_response` below returns just the text and is what most callers
    want. This one exists for the ReAct loop, which has to know whether a
    response finished or was cut off at the token limit.
    """
    import time as _time
    from core import usage_tracker

    augmented_system = (sys_prompt or "").strip()
    if memory_context_text and memory_context_text.strip():
        augmented_system = f"{augmented_system}\n\n{memory_context_text.strip()}".strip()

    # Build the full context string for char-count tracking
    _all_msgs = []
    if history_messages:
        _all_msgs.extend(history_messages)
    _all_msgs.append({"role": "user", "content": prompt_msg})
    context_chars = sum(len(str(m.get("content", ""))) for m in _all_msgs) + len(augmented_system)

    # Global prompt-cache toggle
    from core.cache.prompt_cache import cache_enabled as _cache_enabled
    _prompt_cache = _cache_enabled(current_settings)

    _t0 = _time.time()
    result_text = ""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    stop_reason = None
    _tool_calls: tuple = ()
    _reasoning = ""
    _max_out = int(current_settings.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)

    # One plan for this call, built before dispatch so every branch below asks
    # the same question of the same table. None means "send nothing" — either
    # the model has no reasoning channel, or the person turned it off; both
    # read the same way to a provider adapter.
    from core import reasoning as _reasoning_policy
    _reasoning_plan = _reasoning_policy.plan_for(
        current_model, reasoning_effort, _max_out, current_settings.get("reasoning_models")
    )
    if _reasoning_plan is not None:
        # A budget that does not fit under the response cap is refused by
        # Anthropic and Bedrock outright, so the plan may have raised it.
        _max_out = _reasoning_plan.max_output_tokens

    # ── Response cache (opt-in per call) ─────────────────────────────────────
    # Built BEFORE dispatch so a hit short-circuits the entire LLM round-trip.
    # We rebuild the messages list locally here for hashing — the cache must see
    # the same shape the provider will receive.
    if cache_response:
        from core.cache import response_cache as _resp_cache
        _cache_messages = []
        if history_messages:
            _cache_messages.extend(history_messages)
        _cache_messages.append({"role": "user", "content": prompt_msg})

        _hit = _resp_cache.get_exact(
            current_model, augmented_system, _cache_messages, tools,
        )
        if _hit is None and cache_response_semantic and cache_response_step_id:
            _hit = await _resp_cache.get_semantic_routed(
                cache_response_step_id, current_model,
                augmented_system, str(prompt_msg),
                threshold=cache_response_threshold,
            )
        if _hit is not None:
            _log.debug(f"⚡ response_cache hit ({current_model}) — skipping LLM call")
            try:
                provider = detect_provider_from_model(current_model)
                await usage_tracker.log_usage(
                    model=current_model,
                    provider=provider,
                    input_tokens=0,
                    output_tokens=0,
                    context_chars=context_chars,
                    session_id=session_id,
                    agent_id=agent_id,
                    source=source,
                    run_id=run_id,
                    tool_name=tool_name,
                    latency_seconds=_time.time() - _t0,
                    response_cache_hit=True,
                )
            except Exception as _track_err:
                _log.warning(f"usage_tracker: log_usage (cache hit) failed: {_track_err}")
            return LLMResult(_hit.get("text", ""))

    if mode in ["cloud", "bedrock"]:
        try:
            # Construct messages list for cloud providers that support it
            messages = []
            if history_messages:
                messages.extend(history_messages)
            messages.append({"role": "user", "content": prompt_msg})

            if _is_openai_model(current_model.lower()):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_openai(
                    current_model,
                    [{"role": "system", "content": augmented_system}] + messages,
                    current_settings.get("openai_key"),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                )
            elif current_model.startswith("claude"):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_anthropic(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings.get("anthropic_key"),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                    max_tokens=_max_out,
                    prompt_blocks=prompt_blocks,
                    volatile_tail=volatile_tail,
                )
            elif current_model.startswith("gemini") or current_model.startswith("gemma") or current_model.startswith("lyria"):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_gemini(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings.get("gemini_key"),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                    max_tokens=_max_out,
                )
            elif current_model.startswith("bedrock"):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_bedrock(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings.get("aws_region"),
                    current_settings,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                    max_tokens=_max_out,
                )
                # Bedrock's invoke_model fallback path returns zero tokens; estimate from text.
                if input_tokens == 0 and output_tokens == 0:
                    input_tokens = usage_tracker.estimate_tokens_from_text(
                        augmented_system + " ".join(str(m.get("content", "")) for m in messages)
                    )
                    output_tokens = usage_tracker.estimate_tokens_from_text(result_text)
            elif current_model.startswith("grok"):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_grok(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings.get("grok_key"),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                    max_tokens=_max_out,
                )
            elif current_model.startswith("deepseek"):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_deepseek(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings.get("deepseek_key"),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                    max_tokens=_max_out,
                )
            elif current_model.startswith("synapse."):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_model_gateway(
                    current_model,
                    messages,
                    augmented_system,
                    current_settings,
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                )
            elif current_model.startswith("oaic."):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_v1_compatible(
                    current_model[len("oaic."):],
                    messages,
                    augmented_system,
                    current_settings.get("openai_compatible_base_url", ""),
                    current_settings.get("openai_compatible_key", ""),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                )
            elif current_model.startswith("locv1."):
                result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_v1_compatible(
                    current_model[len("locv1."):],
                    messages,
                    augmented_system,
                    current_settings.get("local_compatible_base_url", ""),
                    current_settings.get("local_compatible_key", ""),
                    tools=tools,
                    images=images,
                    prompt_cache=_prompt_cache,
                    reasoning=_reasoning_plan,
                )
            else:
                return LLMResult("Error: Unknown cloud model selected.")
        except LLMError:
            # LLM errors must propagate — do NOT swallow them.
            # Orchestration engine will catch this and stop execution.
            raise
        except Exception as e:
            return LLMResult(f"Cloud API Error: {str(e)}")

    elif mode == "cli":
        # CLI session providers: build a flat prompt and spawn the local binary.
        # No API key required — uses the user's existing CLI authentication.
        try:
            messages_for_cli = []
            if history_messages:
                messages_for_cli.extend(history_messages)
            messages_for_cli.append({"role": "user", "content": prompt_msg})

            cli_full_prompt = _build_cli_prompt(
                sys_prompt=augmented_system,
                messages=messages_for_cli,
                tools=tools,
            )

            # Load the existing CLI session ID for this agent+session (if any)
            from core.session import get_cli_session_id, save_cli_session_id
            _provider_key = detect_provider_from_model(current_model)
            _existing_cli_sid = await get_cli_session_id(
                session_id or "default", agent_id, _provider_key
            ) if session_id else None

            result_text, input_tokens, output_tokens, new_cli_sid = await call_cli_provider(
                cli_model=current_model,
                full_prompt=cli_full_prompt,
                sys_prompt=augmented_system,
                messages=messages_for_cli,
                cli_session_id=_existing_cli_sid,
            )

            # Persist the CLI session ID so the next turn resumes the same session
            if new_cli_sid and session_id:
                await save_cli_session_id(session_id, agent_id, _provider_key, new_cli_sid)

        except LLMError:
            raise
        except Exception as e:
            return LLMResult(f"CLI Provider Error: {str(e)}")

    elif mode == "hf":
        try:
            messages_for_hf = []
            if history_messages:
                messages_for_hf.extend(history_messages)
            messages_for_hf.append({"role": "user", "content": prompt_msg})

            result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls, _reasoning = await call_huggingface(
                current_model,
                messages_for_hf,
                augmented_system,
                current_settings,
                tools=tools,
                images=images,
            )
        except LLMError:
            raise
        except Exception as e:
            return LLMResult(f"HuggingFace Provider Error: {str(e)}")

    else:
        # Local Ollama
        # Strip the ollama. prefix — the Ollama API expects bare model names
        _ollama_model = current_model[len("ollama."):] if current_model.startswith("ollama.") else current_model
        async with httpx.AsyncClient() as client:
            try:
                # Try specific Ollama Tool Call format if tools are provided
                if tools:
                    _log.debug(f"Calling Ollama /api/chat with tools...")

                    # Construct full message history
                    # 1. System Prompt
                    messages = [{"role": "system", "content": augmented_system}]

                    # 2. History (if available)
                    if history_messages:
                        messages.extend(history_messages)

                    # 3. Current User Message
                    user_msg: dict = {"role": "user", "content": prompt_msg}
                    # Ollama supports images as base64 strings (no data URI prefix)
                    if images:
                        user_msg["images"] = [_parse_data_uri(img)[1] for img in images[:5]]
                    messages.append(user_msg)

                    _chat_body: dict = {
                        "model": _ollama_model,
                        "messages": messages,
                        "tools": tools,
                        "stream": False,
                    }
                    if _reasoning_plan is not None:
                        # Ollama 0.9+ takes a bare boolean here and answers in a
                        # `thinking` field. An older server ignores the key
                        # rather than refusing it, and the `<think>` fallback
                        # below covers that case, so this is safe to send blind.
                        _chat_body["think"] = _reasoning_plan.enabled
                    response = await client.post(
                        f"{_ollama_base_url()}/api/chat",
                        json=_chat_body,
                        timeout=180.0
                    )
                    response.raise_for_status()
                    data = response.json()
                    msg = data.get("message", {})

                    # Check for native tool calls
                    if "tool_calls" in msg and msg["tool_calls"]:
                        # Convert Ollama native tool call to our internal JSON format.
                        # Preserve any text content the model emitted alongside the
                        # call as a [REASONING] preamble (some Ollama models emit
                        # both — qwen3, deepseek-r1 routinely do).
                        _log.debug(f"Native tool call(s) received: {[tc['function']['name'] for tc in msg['tool_calls']]}")
                        call_json = "\n".join(json.dumps({
                            "tool": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"]
                        }) for tc in msg["tool_calls"])
                        narration, _narration_thoughts = _split_think_tags(
                            (msg.get("content") or "").strip())
                        result_text = call_json
                        _reasoning = "\n\n".join(
                            t for t in (msg.get("thinking") or "", _narration_thoughts, narration)
                            if t and t.strip()).strip()
                    else:
                        result_text, _inline_thoughts = _split_think_tags(msg.get("content", ""))
                        _reasoning = "\n\n".join(
                            t for t in (msg.get("thinking") or "", _inline_thoughts)
                            if t and t.strip()).strip()

                    stop_reason = _normalise_stop_reason(data.get("done_reason"))

                    # Ollama eval_count / prompt_eval_count (available when stream=False)
                    input_tokens = data.get("prompt_eval_count", 0) or usage_tracker.estimate_tokens_from_text(prompt_msg)
                    output_tokens = data.get("eval_count", 0) or usage_tracker.estimate_tokens_from_text(result_text)

                else:
                    # Fallback to generate if no tools or tools failed (Old behavior)
                    _log.debug(f"Calling Ollama /api/generate (Legacy Mode)...")

                    prompt_for_generate = prompt_msg
                    if history_messages:
                        prior = _messages_to_transcript(history_messages)
                        if prior:
                            prompt_for_generate = f"Conversation so far:\n{prior}\n\nUser: {prompt_msg}".strip()

                    response = await client.post(
                        f"{_ollama_base_url()}/api/generate",
                        json={
                            "model": _ollama_model,
                            "prompt": prompt_for_generate,
                            "system": augmented_system,
                            "stream": False
                        },
                        timeout=180.0
                    )
                    response.raise_for_status()
                    data = response.json()
                    result_text, _reasoning = _split_think_tags(data.get("response", ""))
                    input_tokens = data.get("prompt_eval_count", 0) or usage_tracker.estimate_tokens_from_text(prompt_for_generate)
                    output_tokens = data.get("eval_count", 0) or usage_tracker.estimate_tokens_from_text(result_text)

            except Exception as e:
                return LLMResult(f"Local Agent Error: {e}")

    # ── Inline thinking tags, whichever provider answered (F32) ──────────────
    # Folded into `reasoning` rather than dropped: the loop shows it as the
    # model's thinking, and a step never writes it into state as the answer.
    result_text, _inline_reasoning = _split_think_tags(result_text)
    if _inline_reasoning:
        _reasoning = f"{_reasoning}\n{_inline_reasoning}".strip() if _reasoning else _inline_reasoning

    # ── Log usage (fire-and-forget, never raises) ────────────────────────────
    try:
        provider = detect_provider_from_model(current_model)
        await usage_tracker.log_usage(
            model=current_model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_chars=context_chars,
            session_id=session_id,
            agent_id=agent_id,
            source=source,
            run_id=run_id,
            tool_name=tool_name,
            latency_seconds=_time.time() - _t0,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
    except Exception as _track_err:
        _log.warning(f"usage_tracker: log_usage failed (non-fatal): {_track_err}")

    # ── Populate response cache on successful call ───────────────────────────
    if cache_response and result_text and not str(result_text).startswith("Error"):
        try:
            from core.cache import response_cache as _resp_cache
            _cache_messages = []
            if history_messages:
                _cache_messages.extend(history_messages)
            _cache_messages.append({"role": "user", "content": prompt_msg})
            _resp_cache.set_exact(
                current_model, augmented_system, _cache_messages, tools,
                text=result_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                ttl_seconds=cache_response_ttl,
                step_id=cache_response_step_id,
            )
            if cache_response_semantic and cache_response_step_id:
                await _resp_cache.set_semantic_routed(
                    cache_response_step_id, current_model,
                    augmented_system, str(prompt_msg),
                    text=result_text,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    ttl_seconds=cache_response_ttl,
                )
        except Exception as _cache_err:
            _log.warning(f"response_cache: set failed (non-fatal): {_cache_err}")

    return LLMResult(result_text, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, stop_reason, _tool_calls,
                     _reasoning)


async def generate_response(*args, **kwargs) -> str:
    """The text of one LLM call.

    Kept as the whole signature's front door: every caller but the ReAct loop
    wants the string and nothing else, and this is what the test suite patches.
    """
    return (await generate_response_ex(*args, **kwargs)).text


# ─── Embedding Helpers ──────────────────────────────────────────────────────

async def embed_batch(texts: list[str], model: str, settings: dict) -> list[list[float]]:
    """Unified embedding function that routes to the appropriate provider.

    Optional settings key ``__embed_output_dim`` (int) is forwarded to
    providers that support dimensionality control (currently Gemini).
    """
    provider = detect_provider_from_model(model)
    output_dim: int | None = settings.get("__embed_output_dim")

    if provider == "openai":
        return await _embed_openai(texts, model, settings.get("openai_key", ""))
    elif provider == "model_gateway":
        clean_model = model[len("synapse."):] if model.startswith("synapse.") else model
        return await _embed_v1_compatible(texts, clean_model, settings.get("model_gateway_base_url", ""), settings.get("model_gateway_key", ""))
    elif provider == "openai_compatible":
        clean_model = model[len("oaic."):] if model.startswith("oaic.") else model
        return await _embed_v1_compatible(texts, clean_model, settings.get("openai_compatible_base_url", ""), settings.get("openai_compatible_key", ""))
    elif provider == "local_compatible":
        clean_model = model[len("locv1."):] if model.startswith("locv1.") else model
        return await _embed_v1_compatible(texts, clean_model, settings.get("local_compatible_base_url", ""), settings.get("local_compatible_key", ""))
    elif provider == "gemini":
        kwargs = {} if output_dim is None else {"output_dim": output_dim}
        return await _embed_gemini(texts, model, settings.get("gemini_key", ""), **kwargs)
    elif provider == "bedrock":
        return await _embed_bedrock(texts, model, settings)
    elif provider == "ollama":
        return await _embed_ollama(texts, model)
    else:
        _log.debug(f"Unknown provider '{provider}' for embedding model '{model}'")
        return [[0.0] * 768 for _ in range(len(texts))]


async def _embed_openai(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    if not api_key:
        return [[0.0] * 1536 for _ in range(len(texts))]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            # OpenAI returns embeddings in the same order as input
            return [item["embedding"] for item in data["data"]]
    except Exception as e:
        _log.error(f"OpenAI embedding failed: {e}")
        return [[0.0] * 1536 for _ in range(len(texts))]


async def _embed_v1_compatible(texts: list[str], model: str, base_url: str, api_key: str, output_dim: int = 768) -> list[list[float]]:
    """Embed via an OpenAI v1-compatible /v1/embeddings endpoint."""
    if not base_url:
        raise LLMError("V1-compatible embedding — base URL not configured")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    endpoint = f"{_normalize_v1_base_url(base_url)}/v1/embeddings"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(endpoint, headers=headers, json={"model": model, "input": texts})
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["data"]]
    except Exception as e:
        raise LLMError(f"V1-compatible embedding failed: {e}")


async def _embed_gemini(
    texts: list[str], model: str, api_key: str, output_dim: int = 768
) -> list[list[float]]:
    if not api_key:
        return [[0.0] * output_dim for _ in range(len(texts))]

    from google import genai
    from google.genai import types

    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=api_key)

    try:
        # Specify output_dimensionality so Gemini returns exactly `output_dim` dims
        # (uses Matryoshka truncation — the first N dims are the best N-dim representation).
        # Without this, the API returns the full 3072-dim vector which exceeds
        # pgvector's 2000-dim HNSW limit.
        result = await asyncio.to_thread(
            _gemini_client.models.embed_content,
            model=model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=output_dim),
        )
        if not result.embeddings:
            return [[0.0] * output_dim for _ in range(len(texts))]
        return [list(em.values) for em in result.embeddings]
    except Exception as e:
        _log.error(f"Gemini embedding failed: {e}")
        return [[0.0] * output_dim for _ in range(len(texts))]


async def _embed_bedrock(texts: list[str], model: str, settings: dict) -> list[list[float]]:
    region = settings.get("aws_region", "us-east-1")
    bedrock = _make_aws_client("bedrock-runtime", region, settings)
    # Strip the 'bedrock.' UI prefix to get the raw Bedrock model ID
    real_model_id = model.replace("bedrock.", "", 1)

    embeddings = []
    for text in texts:
        try:
            if "titan" in real_model_id:
                # Titan Embed v1 and v2 both use inputText; v2 also supports
                # embeddingTypes but the default (float) is fine here.
                payload = json.dumps({"inputText": text})
            elif "cohere" in real_model_id:
                # Cohere Embed v3 via Bedrock expects a texts array + input_type
                payload = json.dumps({
                    "texts": [text[:2048]],
                    "input_type": "search_document",
                    "truncate": "END",
                })
            else:
                # Generic fallback — try the Titan-style inputText payload
                payload = json.dumps({"inputText": text})

            def _run(pid=real_model_id, body=payload):
                return bedrock.invoke_model(
                    body=body,
                    modelId=pid,
                    accept="application/json",
                    contentType="application/json",
                )

            resp = await asyncio.to_thread(_run)
            body_parsed = json.loads(resp.get("body").read())

            if "titan" in real_model_id:
                # Titan v2 may wrap embeddings under embeddingsByType; fall back to
                # top-level 'embedding' key used by v1.
                emb = body_parsed.get("embedding") or []
                embeddings.append(emb)
            elif "cohere" in real_model_id:
                # Cohere returns {"embeddings": [[...]]}
                embs = body_parsed.get("embeddings", [[]])
                embeddings.append(embs[0] if embs else [])
            else:
                embeddings.append(body_parsed.get("embedding", []))

        except Exception as e:
            err_str = str(e)
            if "unknown model" in err_str.lower() or "resourcenotfound" in err_str.lower():
                _log.error(
                    f"Bedrock embedding — model '{real_model_id}' not found in "
                    f"region '{region}'. Ensure the model is enabled in the AWS console "
                    f"and, if it has no on-demand throughput, pick its inference profile "
                    f"(bedrock.us.…) as the embedding model.\nOriginal error: {e}"
                )
            else:
                _log.error(f"Bedrock embedding failed for '{real_model_id}': {e}")
            embeddings.append([])

    return embeddings


async def _embed_ollama(texts: list[str], model: str) -> list[list[float]]:
    model = model[len("ollama."):] if model.startswith("ollama.") else model
    url = f"{_ollama_base_url()}/api/embed"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json={"model": model, "input": texts})
            if resp.status_code != 200:
                # Fallback for older Ollama versions
                url_legacy = f"{_ollama_base_url()}/api/embeddings"
                embeddings = []
                for t in texts:
                    r = await client.post(url_legacy, json={"model": model, "prompt": t})
                    embeddings.append(r.json().get("embedding", []))
                return embeddings
            
            data = resp.json()
            return data.get("embeddings", [])
    except Exception as e:
        _log.error(f"Ollama embedding failed: {e}")
        return [[0.0] * 768 for _ in range(len(texts))]
