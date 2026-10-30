"""The workspace settings Synapse AI may change, and every one it may not, by name (F1264, REACH1; D340 R5).

Naveen, 10-30: "change some settings", on a safe allow-list, each through an approval card; never keys, billing or
the plan, members or roles, API keys, the workspace's MCP server or import and export: those get a link. D291 gave
settings a link only; this amends it for the list below alone, and D292 stands: the change is made in the app only
(`set_settings` is `app_only`), by an owner or admin, with undo like any change.

* `ALLOWED`: each setting Synapse AI may set, with its label, the check its value must pass (the Settings model's
  own type and bounds, a model the workspace can run), what changing it does, and the screen it is on.
* `OUT`: every other field of `models.Settings`, with why it is not Synapse AI's and the screen it is done on.

A test holds that every field of `models.Settings` is in exactly one of the two, so a new setting must be placed
before it ships. A host narrows `ALLOWED` to what its own settings route writes (`set_writable`, cloud's WRITABLE),
and never widens it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

GENERAL, MODELS, KNOWLEDGE, BOARDS = "/settings/general", "/settings/models", "/knowledge", "/boards"


@dataclass(frozen=True)
class Allowed:
    name: str
    label: str
    #: "model", "bool", "int", "number", "choice" or "policy".
    kind: str
    where: str
    #: What turning it on or changing it does, said on the card.
    does: str = ""
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    #: An empty value is allowed and means this (a model left to the default).
    empty: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _a(name: str, label: str, kind: str, where: str, does: str = "", **kw: Any) -> Allowed:
    return Allowed(name, label, kind, where, does, **kw)


_POLICY = ("allow", "confirm", "deny")

ALLOWED: dict[str, Allowed] = {a.name: a for a in (
    _a("agent_name", "Assistant's name", "text", GENERAL, "the name the app's assistant goes by"),
    _a("model", "Default model", "model", MODELS, "every agent without a model of its own runs on it"),
    _a("builder_model", "Synapse AI's model", "model", MODELS, "Synapse AI plans and builds with it",
       empty="the default model"),
    _a("builder_quick_model", "Synapse AI's quick model", "model", MODELS,
       "Synapse AI's small routing steps run on it", empty="Synapse AI's model"),
    _a("judge_model", "Eval judge model", "model", MODELS, "each judged run is scored by it, one call a run",
       empty="the default model"),
    _a("reasoning_effort", "Reasoning effort", "choice", MODELS,
       "how hard a model that can think does so by default; agents may set their own",
       choices=("off", "low", "medium", "high")),
    _a("max_output_tokens", "Longest answer", "int", MODELS,
       "the most a model may write in one answer by default; agents may set their own", low=256, high=262_144,
       empty="automatic"),
    _a("auto_compact_enabled", "Compact long conversations", "bool", GENERAL,
       "when an agent's context fills up, its older tool results move to the Vault first, and only if that isn't "
       "enough is the rest summarised"),
    _a("auto_compact_threshold", "Compact at (characters)", "int", GENERAL,
       "the size at which a conversation is compacted, when the window share is 0 or for a model whose window "
       "isn't known", low=10_000, high=10_000_000),
    _a("auto_compact_window_pct", "Compact at (% of the model's window)", "int", GENERAL,
       "the share of a model's window a conversation may fill before it is compacted; 0 keeps the fixed size",
       low=0, high=90),
    _a("prompt_cache_enabled", "Prompt caching", "bool", GENERAL,
       "a repeated start of a prompt is read from the provider's cache, at its lower price"),
    _a("vault_enabled", "Large response handling", "bool", GENERAL,
       "a tool's very long answer is saved to the Vault and read in parts instead of filling the conversation"),
    _a("vault_threshold", "Large response size (characters)", "int", GENERAL,
       "the size from which a tool or code step's output is saved to the Vault and passed on by reference",
       low=5_000, high=10_000_000),
    _a("turn_limit_mode", "When an agent reaches its turn limit", "choice", GENERAL,
       "ask the person, go on, or stop; an agent's own setting beats it", choices=("", "ask", "continue", "stop"),
       empty="ask in a chat, stop in a run nobody watches"),
    _a("tool_policy_defaults", "Tool policy", "policy", GENERAL,
       "whether agents ask a person before destructive, open-world or undeclared tools, unless an agent sets its own"),
    _a("boards_enabled", "Boards", "bool", GENERAL, "every workflow's agents get a board, and shared boards can be made"),
    _a("knowledge_enabled", "Knowledge", "bool", GENERAL,
       "knowledge bases become searchable by agents; indexing uses the embedding model"),
    _a("messaging_enabled", "Messaging channels", "bool", GENERAL,
       "connected chat channels (Slack, Telegram…) answer messages with their agents"),
    _a("coding_agent_enabled", "Code agents", "bool", GENERAL, "code agents and code steps can run code here"),
    # Limits a self-hosted install sets for itself; on cloud they are the plan's (its host leaves them out).
    _a("team_at_once_ceiling", "Team members at once", "int", GENERAL,
       "the most members of one team that work at the same time", low=1, high=50),
    _a("boards_max_open_cards", "Open cards on a board", "int", BOARDS, "the most open cards one board holds",
       low=10, high=100_000),
    _a("boards_max_shared", "Shared boards", "int", BOARDS, "the most shared boards; 0 has no limit", low=0,
       high=10_000),
    _a("knowledge_max_file_mb", "Largest knowledge file (MB)", "int", KNOWLEDGE, "the largest file a base indexes",
       low=1, high=2_000),
    _a("knowledge_max_file_pages", "Most pages in a file", "int", KNOWLEDGE,
       "the most pages of one document a base indexes", low=1, high=100_000),
    _a("knowledge_crawl_max_depth", "Crawl depth", "int", KNOWLEDGE, "how many links deep a site crawl goes",
       low=0, high=10),
    _a("knowledge_crawl_max_pages", "Pages a crawl reads", "int", KNOWLEDGE, "the most pages one site crawl reads",
       low=1, high=100_000),
    _a("knowledge_crawl_max_hours", "Crawl time (hours)", "number", KNOWLEDGE,
       "the longest one site crawl runs; 0 has no limit", low=0, high=168),
    _a("knowledge_recrawl_days", "Read a site again every (days)", "int", KNOWLEDGE,
       "how often a crawled site is read again", low=1, high=365),
)}

_KEY = "a key or token: entered by a person on its screen, never in a chat"
_PROVIDER = "a provider's address or model list: set where the provider is connected"
_DEPLOY = "how this install runs: its operator's, in Settings or its environment"

#: Every other setting, with why it is not Synapse AI's to change and where it is done.
OUT: dict[str, tuple[str, str]] = {
    **{name: (_KEY, MODELS) for name in (
        "openai_key", "anthropic_key", "gemini_key", "grok_key", "deepseek_key", "moonshot_key", "mistral_key",
        "nvidia_key", "openrouter_key", "qwen_key", "zai_key", "groq_key", "cerebras_key", "together_key",
        "fireworks_key", "minimax_key", "openai_compatible_key", "local_compatible_key", "huggingface_token",
        "bedrock_api_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "cohere_key",
        "voyage_key", "jina_key", "knowledge_rerank_key")},
    **{name: (_PROVIDER, MODELS) for name in (
        "openai_compatible_base_url", "openai_compatible_models", "local_compatible_base_url",
        "local_compatible_models", "openai_compatible_embed_models", "local_compatible_embed_models",
        "huggingface_models", "anthropic_cli_models", "gemini_cli_models", "codex_cli_models",
        "github_copilot_cli_models", "huggingface_max_new_tokens", "aws_region", "ollama_base_url", "mode")},
    **{name: ("how Knowledge indexes and ranks (its internals): set on the Knowledge settings", KNOWLEDGE) for name in (
        "embedding_model", "knowledge_embedding_model", "knowledge_rerank_model", "knowledge_rerank_base_url",
        "knowledge_contextual_model", "knowledge_ingest_concurrency", "knowledge_snapshot_grace_days",
        "knowledge_trace_retention_days", "knowledge_api_search_per_min", "knowledge_crawl_rps_per_host",
        "embed_code")},
    **{name: (_DEPLOY, "/settings/scale") for name in (
        "redis_url", "scale_postgres_url", "scale_mode_enabled", "worker_concurrency", "otlp_endpoint",
        "metrics_token", "max_global_queue_depth", "rate_limit_per_tenant_rps", "pgbouncer_mode")},
    **{name: ("what agents may reach on this machine or its databases: its operator's, never a chat's", GENERAL)
       for name in ("bash_allowed_dirs", "allow_stdio_mcp", "mcp_command_allowlist", "allow_db_write",
                    "transform_runtime", "sql_connection_string")},
    **{name: ("signing in to this install: its owner's, on its screen", GENERAL)
       for name in ("login_enabled", "login_username", "login_password_hash")},
    "n8n_url": ("n8n's connection: set where n8n is connected", "/settings/workspace"),
    "n8n_api_key": (_KEY, "/settings/workspace"),
    "messaging_webhook_base_url": ("the address chat channels' webhooks reach: set where channels are connected",
                                   "/channels"),
    "run_notification_channel_id": ("which channel hears about runs: Synapse AI sets it through the channel's own "
                                    "card (F1263)", "/channels"),
    "global_config": ("the workspace's variables: added and changed on their screen", "/settings/variables"),
    "first_run_models_closed": ("the app's own record that its first-run step was closed", MODELS),
    "clear_secrets": ("a save's instruction to remove a key, never a setting", MODELS),
}

#: What is never Synapse AI's, whatever it is called (Naveen, 10-30): not fields of the Settings model but screens of
#: their own, each refused by name with its link. A host gives its own paths (`set_links`: cloud's billing, members).
NEVER: dict[str, tuple[str, str]] = {
    # Self-hosted has no billing, plan or members' screens ("" here): a host that has them gives their paths.
    "billing": ("billing: the workspace's owner's, on its screen", ""),
    "plan": ("the plan: the workspace's owner's, on the billing screen", ""),
    "members": ("who is in the workspace: an admin's, on its screen", ""),
    "roles": ("members' roles: an admin's, on the members screen", ""),
    "api_keys": ("an API key: made and revoked by a person, never in a chat", "/settings/api_keys"),
    "mcp_server": ("this workspace's own MCP server: turned on by a person, on its screen", "/settings/mcp_server"),
    "import_export": ("importing or exporting the workspace: a person's, on its screen", "/settings/import_export"),
}
#: Other words a planner might use for those.
_NEVER_SAID = {"api_key": "api_keys", "keys": "api_keys", "member": "members", "role": "roles",
               "subscription": "plan", "mcp": "mcp_server", "import": "import_export", "export": "import_export"}

#: A host's own paths for NEVER's screens, {name: path}.
_links: dict[str, str] = {}


def set_links(links: dict[str, str] | None) -> None:
    global _links
    _links = dict(links or {})


#: A host's own list of what its settings route writes (cloud's WRITABLE); None writes all of ALLOWED.
_writable: Callable[[], set[str]] | None = None


def set_writable(fn: Callable[[], set[str]] | None) -> None:
    global _writable
    _writable = fn


def allowed() -> dict[str, Allowed]:
    """What Synapse AI may set on this host: ALLOWED, narrowed by the host's own list, never widened."""
    if _writable is None:
        return dict(ALLOWED)
    host = _writable()
    return {n: a for n, a in ALLOWED.items() if n in host}


def refusal(name: str) -> str:
    """Why a setting is not Synapse AI's to change, with its screen."""
    never = _NEVER_SAID.get(str(name).lower(), str(name).lower())
    if never in NEVER:
        why, where = NEVER[never]
        where = _links.get(never, where)
        return f"`{name}` is {why}." + (f" It is changed at {where}." if where else
                                        " This self-hosted install has no such screen.")
    if name in OUT:
        why, where = OUT[name]
        return f"`{name}` is {why}. It is changed at {where}."
    if name in ALLOWED:
        return f"`{name}` is not changed from Synapse AI on this workspace: it is set at {ALLOWED[name].where}."
    return f"`{name}` is not a setting."


def value_issue(name: str, value: Any) -> str | None:
    """What is wrong with a value for an allowed setting, by the Settings model's own type and bounds; None when
    it is fine. A model's id is checked further by the change (the workspace's models, F1250)."""
    a = ALLOWED[name]
    if a.kind in ("model", "text"):
        ok = isinstance(value, str) and (value.strip() or a.empty) and len(value) <= 200
        return None if ok else f"`{name}` is a model's id as text" if a.kind == "model" else f"`{name}` is text"
    if a.kind == "bool":
        return None if isinstance(value, bool) else f"`{name}` is true or false"
    if a.kind == "choice":
        return None if value in a.choices else f"`{name}` is one of {', '.join(c or '(blank)' for c in a.choices)}"
    if a.kind in ("int", "number"):
        if value is None and a.empty:
            return None
        whole = a.kind == "int"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (whole and not float(value).is_integer()):
            return f"`{name}` is a {'whole ' if whole else ''}number"
        if (a.low is not None and value < a.low) or (a.high is not None and value > a.high):
            return f"`{name}` is from {a.low:g} to {a.high:g}"
        return None
    if a.kind == "policy":
        ok = isinstance(value, dict) and value and all(
            k in ("destructive", "open_world", "undeclared") and v in _POLICY for k, v in value.items())
        return None if ok else ("`tool_policy_defaults` maps destructive, open_world or undeclared to "
                                f"{', '.join(_POLICY)}")
    return f"`{name}` cannot be set here"


def shown(name: str, value: Any) -> str:
    """A setting's value as a person reads it on a card."""
    a = ALLOWED.get(name)
    if value in (None, "") and a is not None and a.empty:
        return a.empty
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in sorted(value.items())) or "(none)"
    return str(value)
