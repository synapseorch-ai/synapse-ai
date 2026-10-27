"""
Pydantic models and data classes used across the backend.
Extracted from server.py for better readability.
"""
from typing import List, Dict, Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

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
    #: A host's own names for `images` (cloud's attachment keys), set by the
    #: worker that resolved them — never by a caller: a private attribute is
    #: not a field, so no request body can set it (F395).
    _image_refs: list[str] | None = PrivateAttr(default=None)
    #: The id a workflow agent's run takes, set by the worker from its job's
    #: enqueuer, who admitted that run (cloud, F417) — never by a caller.
    _workflow_run_id: str | None = PrivateAttr(default=None)
    #: The app's own chat (its stream, an answer's resume, a helper's follow-up), set by those routes — never by a
    #: caller. Only such a turn has an inbox for its helpers' results, and may send one to work in the background
    #: (SUB1a, S4).
    _app_chat: bool = PrivateAttr(default=False)
    #: A turn nobody typed — a helper's result posted back (SUB1a): {kind, helper_id, name, status}. Saved on its
    #: question, which a page draws as the helper's and the model reads as automated, never as the person's words.
    _automated: dict | None = PrivateAttr(default=None)
    #: Helper results a turn starts with: kept by a question it moved past (SUB1a).
    _inbox_seed: list[dict] | None = PrivateAttr(default=None)
    #: "Try again on a stronger model" (F1018): the failed turn this one is pressed from. The server checks it and
    #: picks the model; a caller never names one.
    retry_of: str | None = None
    #: The model the server picked for that one turn, and the turn it tries again — set by the route, never a caller.
    _model_override: str | None = PrivateAttr(default=None)
    _retried_from: str | None = PrivateAttr(default=None)
    _retried_name: str | None = PrivateAttr(default=None)

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

#: The most knowledge bases one agent may answer from: one search covers them
#: all (`core.knowledge.api_models.MAX_KBS_PER_SEARCH`, which a test holds equal).
AGENT_KNOWLEDGE_MAX = 10


class AgentBoards(BaseModel):
    """An agent's Boards section (BOARD1a, D280). The board of the workflow or schedule it runs in is on unless
    turned off, once Boards are on (C2); its shared boards come from each board's own access list."""

    own: bool = True


class AgentTeam(BaseModel):
    """How a Team agent's members work (TEAM1a, D283, Q1). Stored on a `delegate` agent; absent, or
    `one_at_a_time`, is today's delegate (D204). `team`: in parallel, up to `at_once` at a time (capped by
    the install's `team_at_once_ceiling`, Q2a), talking with the lead while they work, a shared task list;
    `task_budget` counts each assignment and each helper (Q8); `spend_limit_usd` is checked before every
    model call the team makes (lead, members, helpers). Read by core/team.py."""

    mode: Literal["one_at_a_time", "team"] = "one_at_a_time"
    at_once: int = Field(default=3, ge=1, le=50)
    task_budget: int = Field(default=6, ge=1, le=50)
    spend_limit_usd: float | None = Field(default=None, gt=0)


class AgentBrowser(BaseModel):
    """A Browser agent's settings (BRW1 v2, DESIGN.md §2.6): where it may go, whose login it uses,
    how many actions a task gets, and whether the person may co-drive."""
    #: Hosts its top-level navigation may reach, redirects included: "portal.example.com" or
    #: "*.example.com". Empty means any public site (chat's reading).
    sites: list[str] = []
    #: A saved website login (`core.website_logins`), loaded into each session it opens.
    login_ref: str | None = None
    action_budget: int = Field(default=40, ge=1, le=200)
    mode: Literal["read_only", "co_drive"] = "read_only"

    @field_validator("sites")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        """Each a host or a `*.` wildcard, lower-case, once; a pasted URL keeps only its host."""
        from urllib.parse import urlparse
        out: list[str] = []
        for raw in value:
            text = (raw or "").strip().lower()
            if not text:
                continue
            host = urlparse(text if "://" in text else f"//{text}").hostname or ""
            wild = text.startswith("*.")
            host = ("*." + host.lstrip("*.")) if wild else host
            label = host[2:] if wild else host
            if not label or "." not in label or "@" in text or any(c in label for c in "/:*"):
                raise ValueError(f"'{raw}' is not a site: give a host such as portal.example.com or *.example.com")
            if host not in out:
                out.append(host)
        return out


class Agent(BaseModel):
    id: str
    name: str = Field(max_length=AGENT_NAME_MAX)
    description: str
    avatar: str = "default"
    type: str = "conversational"  # conversational | analysis | code | orchestrator
    tools: list[str] # ["all"] or ["search_codebase", "get_weather"]
    skills: list[str] = [] # skill names (core/skills.py); roster in prompt, body via load_skill
    memory: bool = False # persistent per-agent memory (core/agent_memory.py); index eager, rest on demand
    # The knowledge bases this agent answers from (Knowledge, D153): while
    # Knowledge is on, the engine offers it `search_knowledge` / `read_knowledge`
    # over the active ones (core/knowledge/tools.py). Ids, in the owner's order.
    knowledge: list[str] = []
    # Boards (D280): read while Boards are on (core/boards/resolve.py). None reads as `own: true`.
    boards: AgentBoards | None = None
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
    # The model its helpers run on (`spawn_subtask`, "Helper model"; SUB1a): None = this agent's own,
    # a model, or `role:quick` = the workspace's "Model for quick steps" (core/helpers.py).
    helper_model: str | None = None
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
    # A delegate's "How members work" (Team, TEAM1a): None = one at a time, as every delegate did.
    team: AgentTeam | None = None
    # A Browser agent's settings (BRW1 v2): None = the defaults (read-only, any public site, 40 actions).
    browser: AgentBrowser | None = None
    # What this agent may do with the tools it holds, by behaviour rather than
    # by name: {"destructive"|"open_world"|"undeclared": "allow"|"confirm"|"deny"}.
    # An absent category inherits the deployment's `tool_policy_defaults`, which
    # itself defaults to allow — so an empty dict is the upgrade-safe default.
    # See core/tool_policy.py. This field is why an agent saved through the API
    # keeps a policy at all: the model drops what it does not declare.
    tool_policy: dict[str, str] = {}

    @field_validator("knowledge")
    @classmethod
    def _knowledge_bases(cls, value: list[str]) -> list[str]:
        """Each id once, in order, blanks dropped; no more than one search covers."""
        kept: list[str] = []
        for kb_id in value:
            kb_id = kb_id.strip()
            if kb_id and kb_id not in kept:
                kept.append(kb_id)
        if len(kept) > AGENT_KNOWLEDGE_MAX:
            raise ValueError(f"An agent answers from at most {AGENT_KNOWLEDGE_MAX} knowledge bases.")
        return kept

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
    first_run_models_closed: bool = False  # the web app's first-run model step was closed (ONB2, D303)
    builder_quick_model: str = ""  # Model for the builder's quick steps (its routing); empty = the builder model (BWA1)
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
    # The hosted OpenAI-style providers (PROV1, D245), one key each.
    moonshot_key: str = ""
    mistral_key: str = ""
    nvidia_key: str = ""
    openrouter_key: str = ""
    qwen_key: str = ""
    zai_key: str = ""
    groq_key: str = ""
    cerebras_key: str = ""
    together_key: str = ""
    fireworks_key: str = ""
    minimax_key: str = ""
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

    # Knowledge — document retrieval (D153). Off until its release; the caps are
    # one for every workspace (the plan's storage limit bounds totals). Read by
    # core/knowledge/config.py.
    knowledge_enabled: bool = False
    knowledge_max_file_mb: int = 100
    knowledge_max_file_pages: int = 2000
    knowledge_crawl_max_depth: int = 3
    knowledge_crawl_max_pages: int = 500
    knowledge_crawl_rps_per_host: float = 1.0
    knowledge_crawl_max_hours: float = 0.0
    knowledge_recrawl_days: int = 7
    knowledge_trace_retention_days: int = 30
    knowledge_api_search_per_min: int = 120
    knowledge_embedding_model: str = ""
    knowledge_rerank_model: str = ""
    knowledge_ingest_concurrency: int = 2
    knowledge_snapshot_grace_days: int = 7
    knowledge_rerank_base_url: str = ""  # a TEI or Infinity server, for a `tei.` rerank model
    knowledge_rerank_key: str = ""  # its key, if it takes one
    knowledge_contextual_model: str = ""  # writes Contextual Retrieval's contexts; blank = `model`
    # Boards — cards of shared work (BOARD1a, D280). Off by default: then no board tool, digest or query
    # reaches any turn. The open-card cap is per board; shared boards per workspace, 0 = no limit. Read by
    # core/boards/config.py; cloud's provider overlays the plan's.
    boards_enabled: bool = False
    boards_max_open_cards: int = 2000
    boards_max_shared: int = 0
    # Teams (TEAM1a, D283, Q2a): the most members a Team agent may have working at once, whatever it asks for.
    # Read by core/team.py; cloud's provider overlays the plan's.
    team_at_once_ceiling: int = 10
    cohere_key: str = ""  # a `cohere.` rerank model's key
    voyage_key: str = ""  # a `voyage.` rerank model's key
    jina_key: str = ""  # a `jina.` rerank model's key


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
    # The knowledge bases the agent answers from, as {name, description} (Knowledge,
    # B1b): the writer replaces its "Tool vs. Knowledge" rule for such an agent.
    knowledge_bases: list[dict] = []


class GoogleCredsRequest(BaseModel):
    content: str # Raw JSON string or dict
