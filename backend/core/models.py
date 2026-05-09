"""
Pydantic models and data classes used across the backend.
Extracted from server.py for better readability.
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel


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
    # Base64 data-URI images (e.g. "data:image/png;base64,..."). Max 5 per request.
    images: list[str] = []

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


class Agent(BaseModel):
    id: str
    name: str
    description: str
    avatar: str = "default"
    type: str = "conversational"  # conversational | analysis | code | orchestrator
    tools: list[str] # ["all"] or ["search_codebase", "get_weather"]
    repos: list[str] = [] # list of repo IDs for code agents
    db_configs: list[str] = [] # list of db config IDs for code agents
    system_prompt: str
    orchestration_id: str | None = None  # for orchestrator type agents
    model: str | None = None  # per-agent model override (None = use default)
    provider: str | None = None  # auto-detected from model name
    max_turns: int | None = None  # per-agent turn limit override (None = use global default of 30)

class Repo(BaseModel):
    id: str
    name: str
    path: str
    description: str = ""
    included_patterns: list[str] = ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.rs", "*.go", "*.java", "*.md", "*.html", "*.vue", "*.css", "*.scss", "*.cpp", "*.c"]
    excluded_patterns: list[str] = [".*", "node_modules", "__pycache__", "venv", ".git", "*.pyc"]
    last_indexed: str | None = None
    status: str = "pending" # pending | indexing | indexed | error
    file_count: int = 0
    embedding_model_provider: str = "gemini"
    error_message: str | None = None

class AgentActiveRequest(BaseModel):
    agent_id: str


class Settings(BaseModel):
    agent_name: str
    model: str = "mistral" # Default model (Ollama or Cloud)
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
    bedrock_api_key: str = ""  # e.g. ABSK... (Amazon Bedrock API key)
    # Optional: required for some Bedrock models that don't support on-demand throughput.
    # Can be an inference profile ID or full ARN.
    bedrock_inference_profile: str = ""
    # Optional: embedding model used for long-term memory when mode == bedrock
    embedding_model: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_region: str = "us-east-1"
    sql_connection_string: str = ""
    n8n_url: str = "http://localhost:5678"
    n8n_api_key: str = ""
    n8n_table_id: str = ""
    global_config: dict[str, str] = {}
    vault_enabled: bool = True
    vault_threshold: int = 100000
    auto_compact_enabled: bool = False
    auto_compact_threshold: int = 100000
    allow_db_write: bool = False  # If False, only SELECT/SHOW/DESCRIBE queries allowed
    embed_code: bool = False  # If False, search_codebase is disabled and postgres not required
    bash_allowed_dirs: list[str] = []  # Extra dirs the bash tool can run commands in (repos + vault always included)
    report_agent_enabled: bool = True
    coding_agent_enabled: bool = True
    messaging_enabled: bool = True
    login_enabled: bool = False
    login_username: str = ""
    login_password_hash: str = ""


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


class GoogleCredsRequest(BaseModel):
    content: str # Raw JSON string or dict
