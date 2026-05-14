"""
Agent management endpoints (CRUD + active agent).
"""
import os
import json
import datetime
import zoneinfo

from fastapi import APIRouter, HTTPException

from core.models import Agent, AgentActiveRequest, GeneratePromptRequest
from core.config import DATA_DIR, load_settings
from core.json_store import JsonStore
from core.llm_providers import generate_response as llm_generate_response, detect_mode_from_model

router = APIRouter()

_agents_store = JsonStore(os.path.join(DATA_DIR, "user_agents.json"), cache_ttl=2.0)

# Module-level state
active_agent_id: str | None = None


def load_user_agents() -> list[dict]:
    return _agents_store.load()


def save_user_agents(agents: list[dict]):
    _agents_store.save(agents)


def get_active_agent_data():
    agents = load_user_agents()
    if active_agent_id:
        for a in agents:
            if a["id"] == active_agent_id:
                return a
    if agents:
        return agents[0]
    raise RuntimeError("No agents configured.")


@router.get("/api/agents")
async def get_agents():
    return load_user_agents()


@router.post("/api/agents")
async def create_agent(agent: Agent):
    agents = load_user_agents()
    # Check if exists
    for i, a in enumerate(agents):
        if a["id"] == agent.id:
            agents[i] = agent.dict()  # Update
            save_user_agents(agents)
            return agent

    agents.append(agent.dict())
    save_user_agents(agents)
    return agent


@router.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    global active_agent_id
    agents = load_user_agents()
    agents = [a for a in agents if a["id"] != agent_id]
    save_user_agents(agents)
    if active_agent_id == agent_id:
        active_agent_id = None
    return {"status": "success"}


@router.get("/api/agents/active")
async def get_active_agent_endpoint():
    try:
        agent = get_active_agent_data()
        return {"active_agent_id": agent["id"]}
    except RuntimeError:
        return {"active_agent_id": None}


@router.post("/api/agents/active")
async def set_active_agent_endpoint(req: AgentActiveRequest):
    global active_agent_id
    # Validate
    agents = load_user_agents()
    ids = [a["id"] for a in agents]
    if req.agent_id not in ids:
        raise HTTPException(status_code=404, detail="Agent not found")

    active_agent_id = req.agent_id
    print(f"Active Agent switched to: {active_agent_id}")
    return {"status": "success", "active_agent_id": active_agent_id}


PROMPT_WRITER_SYSTEM = """You are an expert AI system prompt architect. Your goal: generate precise, production-grade system prompts that change model behavior — not describe it.

━━━ PHASE 1: SILENT ANALYSIS (never output) ━━━
Before writing, reason through:
- **Real Intent:** What is the user actually trying to accomplish? Look past the label.
- **Tool Clusters:** Group tools into capability sets. Identify what the agent can and cannot do.
- **Agent Type:**
  - `conversational` — multi-turn; handle context shifts and follow-ups
  - `code` — precision required; read before write, cite paths/lines
  - `orchestrator` — decompose tasks, manage sub-agent handoffs, synthesize results
  - `delegate` — dynamic router; receives queries and routes to sub-agents via delegate_to_agent tool; focus on routing logic, clear task framing, and synthesizing results
- **Failure Modes:** Where will this agent most likely hallucinate, go off-scope, or stall?

━━━ PHASE 2: GENERATE THE SYSTEM PROMPT ━━━
Always include ALL of the following sections:

### ROLE & MISSION
One paragraph: who the agent is, what it exists to do, and what success looks like. Ground it in the user's real intent, not the surface label.

### CORE CAPABILITIES
What the agent can do, grouped by capability cluster (e.g., "web research", "data processing"). Never list raw tool names — describe what they enable.

### REASONING
How the agent should think before acting:
- State the chosen approach and why before executing.
- On judgment calls, surface the tradeoff explicitly.
- On ambiguous or incomplete tool results, acknowledge uncertainty — never fill gaps with assumptions.
- After multi-step tasks, provide a brief "what was done and why" summary.

### METHODOLOGY
- **Decomposition:** Break complex requests into ordered steps before acting.
- **Tool vs. Knowledge:** Prefer tools for facts, data, and file contents. Use knowledge only for general reasoning or when tools are unavailable.
- **Decision Rule:** Choose the simplest approach that fully solves the problem.
- **Iteration:** Refine when output quality matters; accept "good enough" for quick lookups.

### OUTPUT FORMAT
- Default structure: define a clear response template suited to the agent type.
- Tone: set based on purpose (technical, conversational, formal).
- Verbosity: short for simple queries, detailed for complex ones — never pad.
- Formatting rules: when to use tables, bullets, code blocks, or plain prose.
- Error/partial states: define a distinct format for failures and incomplete results.

### CONSTRAINTS
- **Data Integrity:** Never fabricate data, quotes, stats, or file contents. If unavailable, say so.
- **Scope:** Define in-scope vs. out-of-scope. On out-of-scope requests, acknowledge and redirect to what you can do.
- **Tool Discipline:** Never claim an action without calling the relevant tool. Never assume tool output.
- **Hallucination Triggers:** Identify the top 2–3 scenarios where this agent is most likely to hallucinate, and prescribe the fallback behavior for each.

### EDGE CASES
- **Ambiguity:** Define when to ask for clarification vs. make a best-guess (always state the guess).
- **Tool Failures:** What to do when a tool errors or returns unexpected results.
- **Partial Completion:** Deliver what's possible; clearly state what's missing and why.
- **Constraint Conflicts:** How to handle requests that violate constraints.

━━━ STRICT RULES ━━━
- Do NOT include a tools section, tool-calling format, or tool names — injected at runtime.
- Do NOT include date/time context — also injected automatically.
- Every sentence must change behavior. Cut any line that only restates what the agent is.
- The prompt must be self-contained and usable as-is.
- Use markdown with clear `###` section headers.

YOUR RESPONSE = THE SYSTEM PROMPT. Nothing before it. Nothing after it. No commentary, no labels, no wrapping."""


@router.get("/api/agent-types")
async def get_agent_types():
    """Returns available agent types based on enabled features in settings."""
    s = load_settings()
    types = [
        {"value": "conversational", "label": "Conversational", "description": "General-purpose agent with configurable tools."},
        {"value": "orchestrator", "label": "Orchestrator", "description": "Multi-agent orchestration — deployed from the Orchestrations tab."},
        {"value": "delegate", "label": "Delegate", "description": "Routes queries to sub-agents dynamically. Usable standalone or as an agent step in orchestrations."},
    ]
    if s.get("coding_agent_enabled"):
        types.insert(2, {"value": "code", "label": "Code", "description": "Automatically includes search_codebase for semantic code search."})
    return {"types": types}


def _categorize_tools(tools: list[str]) -> str:
    """Group flat tool list into capability clusters for better LLM understanding."""
    categories = {
        "Web & Browser": [],
        "File System & Code": [],
        "Data Processing": [],
        "Communication & Workspace": [],
        "Database": [],
        "Persistence & Vault": [],
        "Reasoning & Planning": [],
        "Other": [],
    }

    keyword_map = {
        "Web & Browser": ["browser_", "web_", "parse_pdf", "parse_url"],
        "File System & Code": ["read_file", "read_text_file", "read_multiple_files", "read_file_by_lines",
                               "write_file", "list_directory", "directory_tree", "search_files",
                               "get_file_info", "list_allowed_directories", "list_directory_with_sizes",
                               "search_codebase", "grep", "glob", "edit_file", "create_file"],
        "Data Processing": ["execute_python", "parse_xlsx", "parse_csv", "search_embedded_report"],
        "Communication & Workspace": ["gmail_", "gcal_", "gdrive_", "slack_", "jira_", "send_", "google_"],
        "Database": ["run_sql", "list_tables", "get_table_schema", "db_"],
        "Persistence & Vault": ["vault_", "memory_"],
        "Reasoning & Planning": ["sequentialthinking"],
    }

    for tool_entry in tools:
        tool_name = tool_entry.split(" - ")[0].strip().lower()
        placed = False
        for category, keywords in keyword_map.items():
            if any(tool_name.startswith(k) or k in tool_name for k in keywords):
                categories[category].append(tool_entry)
                placed = True
                break
        if not placed:
            categories["Other"].append(tool_entry)

    sections = []
    for category, cat_tools in categories.items():
        if cat_tools:
            tool_lines = "\n".join(f"  - {t}" for t in cat_tools)
            sections.append(f"**{category}:**\n{tool_lines}")

    return "\n\n".join(sections) if sections else "No specific tools selected."


AGENT_TYPE_CONTEXT = {
    "conversational": (
        "This is a CONVERSATIONAL agent — it interacts directly with users in multi-turn dialogue. "
        "It should handle follow-up questions, context shifts, and clarification requests gracefully. "
        "The prompt should optimize for helpful, accurate, and well-structured responses."
    ),
    "code": (
        "This is a CODE agent — it works with codebases, repositories, and technical tasks. "
        "It has access to semantic code search across indexed repos, file reading, and grep/glob. "
        "The prompt should emphasize: read before modifying, cite file paths and line numbers, "
        "verify assumptions by reading code rather than guessing, and technical precision."
    ),
    "orchestrator": (
        "This is an ORCHESTRATOR agent — it coordinates multi-step workflows across sub-agents. "
        "It receives context from previous steps and must produce structured outputs for downstream steps. "
        "The prompt should emphasize: clear task decomposition, structured output formats, "
        "and awareness that its output feeds into other agents."
    ),
    "delegate": (
        "This is a DELEGATE agent — it acts as a dynamic router that receives user queries and decides "
        "which sub-agent to hand the task to. It has access to a delegate_to_agent tool that runs a "
        "sub-agent's full ReAct loop. After receiving a sub-agent's result, the delegate can either "
        "delegate to another agent or produce a final synthesized response. The prompt should emphasize: "
        "intelligent task routing, understanding each sub-agent's strengths, clear task instructions "
        "when delegating, and synthesizing results from multiple agents."
    ),
}


@router.post("/api/agents/generate-prompt")
async def generate_agent_prompt(req: GeneratePromptRequest):
    """Generate a comprehensive system prompt from a description using the configured LLM."""
    settings = load_settings()
    model = settings.get("model", "mistral")
    mode = detect_mode_from_model(model)

    now = datetime.datetime.now(zoneinfo.ZoneInfo("UTC"))
    current_datetime = now.strftime("%B %d, %Y %I:%M %p UTC")

    # Build structured tool context
    tools_section = ""
    if req.tools:
        categorized = _categorize_tools(req.tools)
        tools_section = (
            f"\n\n━━━ AVAILABLE TOOLS (grouped by capability) ━━━\n"
            f"{categorized}\n"
            f"\nThe agent should leverage these tools strategically. "
            f"Understand what workflows become possible by COMBINING tools "
            f"(e.g., browser tools + vault = research with persistent notes; "
            f"file reading + execute_python = data analysis pipeline). "
            f"Also note what the agent CANNOT do based on which tools are absent."
        )

    # Build agent type context
    type_context = AGENT_TYPE_CONTEXT.get(req.agent_type, "")

    # Build existing prompt section for refinement
    existing_section = ""
    if req.existing_prompt.strip():
        existing_section = (
            f"\n\n━━━ EXISTING PROMPT TO REFINE ━━━\n"
            f"The user already has a system prompt and wants it improved. "
            f"Preserve what works well, fix weaknesses, and enhance with the sections "
            f"defined in your instructions. Here is the current prompt:\n"
            f"---\n{req.existing_prompt.strip()}\n---"
        )

    # Build delegate sub-agents section (only for delegate-type agents)
    delegates_section = ""
    if req.agent_type == "delegate" and req.agents:
        agent_lines = "\n".join(
            f"  - [{a.get('id', '')}] {a.get('name', '')} ({a.get('type', 'conversational')})"
            + (f": {a['description']}" if a.get('description') else "")
            for a in req.agents
        )
        delegates_section = (
            f"\n\n━━━ AVAILABLE SUB-AGENTS (delegation targets) ━━━\n"
            f"This delegate agent can route tasks to the following sub-agents:\n"
            f"{agent_lines}\n"
            f"\nThe generated prompt should reference these agents by name where relevant, "
            f"describe how to decide which agent to delegate to, and explain how to synthesize "
            f"results when multiple agents are involved."
        )

    user_message = (
        f"Current Date & Time: {current_datetime}\n\n"
        f"━━━ AGENT TYPE ━━━\n"
        f"Type: {req.agent_type}\n"
        f"{type_context}\n\n"
        f"━━━ USER'S DESCRIPTION ━━━\n"
        f"{req.description}\n"
        f"\nAnalyze this description carefully. What is the user's REAL goal? "
        f"What problem are they trying to solve? What would make this agent "
        f"genuinely useful vs. just technically correct?"
        f"{tools_section}"
        f"{delegates_section}"
        f"{existing_section}"
    )

    try:
        result = await llm_generate_response(
            prompt_msg=user_message,
            sys_prompt=PROMPT_WRITER_SYSTEM,
            mode=mode,
            current_model=model,
            current_settings=settings,
        )
        return {"system_prompt": result}
    except Exception as e:
        print(f"Error generating prompt: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate prompt: {str(e)}")
