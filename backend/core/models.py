"""
Pydantic models and data classes used across the backend.
Extracted from server.py for better readability.
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from core.images import checked_images


class ChatRequest(BaseModel):
    message: str
    # Client-generated ID for scoping short-term context. Frontend should generate a new
    # one on each reload so each tab/reload is a fresh session.
    session_id: str | None = None
    # The active agent ID — sent by the frontend on every request so the backend
    # doesn't rely on the global variable (which resets on uvicorn reload).
    agent_id: str | None = None
    # Optional ephemeral client-side state we want the server/agent to reuse.
    client_state: dict[str, Any] | None = None
    # Base64 data-URI images (e.g. "data:image/png;base64,..."). Max 5 per
    # request — and now actually enforced. The limit used to live in this
    # comment and in `[:5]` slices inside each provider's content builder, so
    # a caller reaching the API directly was bounded by nothing at all.
    images: list[str] = []

    _check_images = checked_images()

class ChatResponse(BaseModel):
    response: str
    intent: str = "chat"
    data: Any | None = None
    tool_name: str | None = None


class DBConfig(BaseModel):
    id: str
    name: str
    db_type: str  # postgres, mysql, sqlite, mssql
    connection_string: str
    description: str = ""
    schema_info: str = ""  # Cached schema summary (populated via refresh)
    last_tested: str | None = None
    status: str = "untested"  # untested | connected | error
    error_message: str | None = None


#: The longest agent name any path will write (F217, Naveen 2026-09-26). No path
#: had one: a 203-character name was saved, and the chat's empty state spelled
#: it out 1,742 px wide, off both edges of the page.
AGENT_NAME_MAX = 100

#: The range a response cap may be set in (F263). Below the floor a model cannot
#: finish a sentence and a tool call; above the ceiling no model we route to
#: writes that much in one response.
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 262_144


class Agent(BaseModel):
    id: str
    name: str = Field(max_length=AGENT_NAME_MAX)
    description: str
    avatar: str = "default"
    type: str = "conversational"  # conversational | analysis | code | orchestrator
    tools: list[str] # ["all"] or ["search_codebase", "get_weather"]
    skills: list[str] = [] # skill names (core/skills.py); roster in prompt, body via load_skill
    memory: bool = False # persistent per-agent memory (core/agent_memory.py); index eager, rest on demand
    # The native task list (`update_tasks`), on unless turned off; and the whole
    # default tool set, kept unless skipped. The engine read both all along and
    # this model dropped both, so the editor's Planning switch re-ticked after
    # every save and a builder's `planning: false` was erased by the next (F265).
    planning: bool = True
    skip_default_tools: bool = False
    repos: list[str] = [] # list of repo IDs for code agents
    db_configs: list[str] = [] # list of db config IDs for code agents
    system_prompt: str
    orchestration_id: str | None = None  # for orchestrator type agents
    model: str | None = None  # per-agent model override (None = use default)
    provider: str | None = None  # auto-detected from model name
    max_turns: int | None = None  # per-agent turn limit override (None = use global default of 30)
    # "off" | "low" | "medium" | "high"; None = inherit the workspace setting.
    # Only reaches models with a native reasoning channel (see core/reasoning.py);
    # everything else keeps the [REASONING] prompt convention and ignores it.
    reasoning_effort: str | None = None
    # The most this agent's model may write in one response, thinking included
    # (F263); None = the workspace's `max_output_tokens`. A model that thinks is
    # still given room past its thinking (core/reasoning.py).
    max_output_tokens: int | None = Field(default=None, ge=MIN_OUTPUT_TOKENS, le=MAX_OUTPUT_TOKENS)
    delegate_agent_ids: list[str] = []  # for delegate-type agents; empty = allow all
    # What this agent may do with the tools it holds, by behaviour rather than
    # by name: {"destructive"|"open_world"|"undeclared": "allow"|"confirm"|"deny"}.
    # An absent category inherits the deployment's `tool_policy_defaults`, which
    # itself defaults to allow — so an empty dict is the upgrade-safe default.
    # See core/tool_policy.py. This field is why an agent saved through the API
    # keeps a policy at all: the model drops what it does not declare.
    tool_policy: dict[str, str] = {}

class Repo(BaseModel):
    id: str
    name: str
    path: str
    description: str = ""
    included_patterns: list[str] = ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.rs", "*.go", "*.java", "*.md", "*.html", "*.vue", "*.svelte", "*.css", "*.scss", "*.cpp", "*.c"]
    excluded_patterns: list[str] = [".*", "node_modules", "__pycache__", "venv", ".git", "*.pyc"]
    last_indexed: str | None = None
    status: str = "pending" # pending | indexing | indexed | error
    file_count: int = 0
    error_message: str | None = None

class AgentActiveRequest(BaseModel):
    agent_id: str


class Settings(BaseModel):
    """What `POST /api/settings` accepts — a patch, merged over what is stored.

    Unknown fields are refused (422, naming them) rather than dropped (S11):
    `allow_stdio_mcp` and `mcp_command_allowlist` were missing here, so saving
    either answered 200 and changed nothing, and the stdio knob the security
    advisory tells operators to turn off could never be turned off. A field a
    screen sends belongs in this model; one nobody should send is an error.
    """
    model_config = ConfigDict(extra="forbid")

    # A default only so a patch need not carry it: the route merges with
    # `exclude_unset`, so an unsent field never overwrites what is stored.
    agent_name: str = "Synapse"
    model: str = "mistral" # Default model (Ollama or Cloud)
    builder_model: str = ""  # Model for the AI builder; empty = use `model`
    judge_model: str = ""    # Model for eval scoring; empty = use `model`
    reasoning_effort: str = "medium"  # workspace default; agents may override
    # The most a model may write in one response (F263). None = automatic: each
    # provider as it always was (8,192 where one was sent, none where none was).
    # Read by `llm_providers.generate_response_ex`, which had been reading it all
    # along while this model refused it with a 422. Agents may override.
    max_output_tokens: int | None = Field(default=None, ge=MIN_OUTPUT_TOKENS, le=MAX_OUTPUT_TOKENS)
    mode: str = "local" # "local" | "cloud" | "bedrock"
    openai_key: str = ""
    anthropic_key: str = ""
    gemini_key: str = ""
    grok_key: str = ""  # xAI Grok API key (starts with 'xai-')
    deepseek_key: str = ""  # DeepSeek API key
    openai_compatible_key: str = ""  # API key for OpenAI-compatible cloud (OpenRouter, Together, etc.)
    openai_compatible_base_url: str = ""  # Base URL for OpenAI-compatible cloud (e.g. https://openrouter.ai/api)
    openai_compatible_models: str = ""  # Comma-separated model names for OpenAI-compatible cloud
    local_compatible_base_url: str = ""  # Base URL for local v1-compatible (vLLM, LM Studio, etc.)
    local_compatible_key: str = ""  # Optional API key for local v1-compatible
    local_compatible_models: str = ""  # Comma-separated model names for local v1-compatible
    openai_compatible_embed_models: str = ""  # Comma-separated embedding model names for OpenAI-compatible cloud
    local_compatible_embed_models: str = ""  # Comma-separated embedding model names for local v1-compatible
    huggingface_token: str = ""  # Optional HF access token (required for gated models like Llama, Gemma)
    huggingface_models: str = ""  # Comma- or newline-separated HF model IDs (e.g. Qwen/Qwen2.5-7B-Instruct)
    # Comma-separated custom model names for the local CLI session providers.
    # Each entry becomes a selectable cli.<provider>.<name> model (e.g. "gpt-5.4"
    # → cli.codex.gpt-5.4), passed to the CLI via its -m/--model flag at call time.
    anthropic_cli_models: str = ""
    gemini_cli_models: str = ""
    codex_cli_models: str = ""
    github_copilot_cli_models: str = ""
    huggingface_max_new_tokens: int = 1024  # Max tokens to generate per HF call
    # Transform step Python execution runtime: "docker" (sandboxed, default) or
    # "host" (unsandboxed subprocess on host — needed for torch/GPU workloads,
    # removes the sandbox security boundary).
    transform_runtime: str = "docker"
    bedrock_api_key: str = ""  # e.g. ABSK... (Amazon Bedrock API key)
    # Optional: embedding model used for long-term memory when mode == bedrock
    embedding_model: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_region: str = "us-east-1"
    sql_connection_string: str = ""
    n8n_url: str = "http://localhost:5678"
    n8n_api_key: str = ""
    global_config: dict[str, str] = {}
    vault_enabled: bool = True
    vault_threshold: int = 100000
    # Read by core/cache/prompt_cache.py. Missing here, so it could not be
    # changed through the settings route (F76).
    prompt_cache_enabled: bool = True
    # Where Ollama answers; empty uses the OLLAMA_BASE_URL env var (F76).
    ollama_base_url: str = ""
    auto_compact_enabled: bool = False
    auto_compact_threshold: int = 100000
    allow_db_write: bool = False  # If False, only SELECT/SHOW/DESCRIBE queries allowed
    # The deployment-wide floor for tool policy, in the same shape as an agent's
    # own `tool_policy` and overridden by it per category. Empty means allow
    # everything, which is what every existing agent already does.
    tool_policy_defaults: dict[str, str] = {}
    embed_code: bool = False  # If False, search_codebase is disabled and postgres not required
    bash_allowed_dirs: list[str] = []  # Extra dirs the bash tool can run commands in (repos + vault always included)
    # stdio MCP servers launch local commands (GHSA-3j67-x3j8-r32x). Allowed by
    # default for a local single-user install; scale mode forces it off
    # (core/mcp_client.stdio_mcp_allowed). Settable only since S11.
    allow_stdio_mcp: bool = True
    # When non-empty, only these command basenames may run as stdio servers
    # (e.g. ["npx", "uvx"]). Empty allows any command on PATH.
    mcp_command_allowlist: list[str] = []
    # Not a setting: which secrets this save removes (F201). A secret is never
    # served back, so a blank one means "unchanged" and removal is explicit.
    clear_secrets: list[str] = []
    # An install choice: read once at startup to start the code indexer
    # (core/server.py), so changing it takes a restart. Asked by the installer.
    coding_agent_enabled: bool = True
    messaging_enabled: bool = True
    # Public HTTPS base a platform can reach this deployment at (a domain or a
    # tunnel). Only webhook-mode messaging channels need it: the Telegram
    # adapter registers `{base}/api/messaging/telegram/webhook/{id}` with
    # setWebhook, and the UI shows the equivalent URL to paste into the Slack
    # and Discord portals. Empty = polling modes only.
    messaging_webhook_base_url: str = ""
    login_enabled: bool = False
    login_username: str = ""
    login_password_hash: str = ""

    # Run notifications: messaging channel id to mirror run notifications to
    # (human-input pauses, completions, failures). Empty = in-app only.
    run_notification_channel_id: str = ""

    # Scale / distributed execution settings
    redis_url: str = ""
    scale_postgres_url: str = ""
    scale_mode_enabled: bool = False
    worker_concurrency: int = 10
    otlp_endpoint: str = ""
    metrics_token: str = ""
    max_global_queue_depth: int = 1_000_000
    rate_limit_per_tenant_rps: int = 1000
    pgbouncer_mode: bool = False


class PersonalAddress(BaseModel):
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""


class PersonalDetails(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone_number: str = ""
    address: PersonalAddress = PersonalAddress()


class AddMCPServerRequest(BaseModel):
    name: str
    label: str = ""                # human-friendly display name (falls back to name if empty)
    server_type: str = "stdio"     # "stdio" | "remote"
    # stdio fields
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    # remote fields
    url: str = ""
    token: str = ""                # pre-auth bearer token (PAT); empty = start OAuth


class GeneratePromptRequest(BaseModel):
    description: str
    agent_type: str = "conversational"
    tools: list[str] = []
    existing_prompt: str = ""
    agents: list[dict] = []  # list of {id, name, description, type} — for delegate agents


class GoogleCredsRequest(BaseModel):
    content: str # Raw JSON string or dict
