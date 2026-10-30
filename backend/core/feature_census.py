"""Every screen of the app, and what Synapse AI can do there (F1256; TRI1's triage part 3, Naveen's pick, 10-30).

"Create an eval for my curiosity post generator" built a workflow that imitated an eval: nothing tied the app's
screens to what Synapse AI reaches, so a feature it could not reach was rebuilt from workflow parts and the person was
never told. "Synapse AI can do anything in the product" was a rule, never a mechanism; this is the mechanism.

One row per nav entry (`frontend/src/lib/nav.ts`, both rails), by its id. A row names the tools that reach the screen,
the product registry's or the builder's own, or a `read_workspace` topic as `read_workspace:<topic>`, and says
what they do. A row also says what is not done from Synapse AI yet (`cant`), with the screen it is done on. A row has
one or both. Tests hold the table to nav.ts (every screen has a row, no row is for a screen that is gone) and to the
tools (every name is one that exists), so it cannot go stale. The capabilities text Synapse AI reads
(`builder_help.SELF_HOSTED_CAPABILITIES`) and the planner's `not_here` are made from it. A host adds or replaces rows
for its own screens (cloud's census), and keeps its own general limits.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Screen:
    id: str
    label: str
    href: str
    does: str = ""
    tools: tuple[str, ...] = ()
    #: What is not done from Synapse AI yet: said, with this screen to open, never imitated with a workflow.
    cant: str = ""


def _s(id: str, label: str, href: str, does: str = "", tools: tuple[str, ...] = (), cant: str = "") -> Screen:
    return Screen(id, label, href, does, tools, cant)


#: The self-hosted app's screens (nav.ts's HUB_NAV, then SETTINGS_NAV), in the rail's order.
CENSUS: tuple[Screen, ...] = (
    _s("dashboard", "Home", "/dashboard", "says what happened today and what waits on the person",
       ("read_workspace:today", "read_workspace:waiting", "summarise_activity", "list_waiting")),
    _s("runs", "Runs", "/runs", "lists runs, reads one step by step, answers a run's question, stops one, says why "
       "one failed and resumes it once fixed",
       ("list_runs", "get_run", "read_workspace:run", "answer_run", "cancel_run", "resume_run")),
    _s("logs", "Logs", "/logs", "reads what a run or an agent did, and why a run failed: its step, its error, the "
       "class of failure and the change that fixes it",
       ("read_workspace:run", "read_workspace:agent_activity", "agent_activity")),
    _s("evals", "Evals", "/evals",
       cant="writing a rubric, making golden test cases, running an eval suite and reading eval scores"),
    _s("usage", "Usage", "/usage", "says what workflows and models have cost, and what the plan has left",
       ("get_usage", "read_workspace:usage", "read_workspace:plan")),
    _s("orchestrations", "Workflows", "/workflows", "builds, changes, checks, runs and deletes workflows",
       ("create_orchestration", "update_orchestration", "add_single_step", "update_step", "remove_step",
        "validate_orchestration", "run_orchestration", "delete_orchestration", "read_workspace:workflow")),
    _s("schedules", "Schedules", "/schedules", "makes, changes, pauses, runs now and deletes schedules, sets what one "
       "does with a time it missed and points one at another workflow or agent",
       ("create_schedule", "update_schedule", "set_schedule_enabled", "trigger_schedule", "delete_schedule")),
    _s("templates", "Templates", "/templates", cant="browsing and importing the ready-made template packs"),
    _s("agents", "Agents", "/agents", "makes, changes and deletes agents, talks to one, and says what one has done",
       ("create_agent", "update_agent", "delete_agent", "chat_with_agent", "read_workspace:agent_activity")),
    _s("skills", "Skills", "/skills", "writes skills and gives them to agents",
       ("create_skill", "update_skill", "attach_skills", "list_skills")),
    _s("browser", "Browser", "/browser", "lists the kept sign-ins and recorded tasks, uses or drops a fix a run kept "
       "on a recording, renames and deletes a recording",
       ("list_browser_sessions", "list_recordings", "get_recording", "delete_recording", "approve_recording_edit",
        "discard_recording_edit", "rename_recording"),
       cant="signing in to a site or recording a task, which a person does in the app's browser"),
    _s("vault", "Vault", "/vault", cant="browsing, uploading and organising the Vault's files"),
    _s("knowledge", "Knowledge", "/knowledge", "makes knowledge bases, adds files, pages and notes, and searches them",
       ("create_knowledge_base", "add_knowledge_file", "add_knowledge_pages", "add_knowledge_note",
        "search_knowledge", "update_knowledge_base", "delete_knowledge_base", "list_knowledge_documents")),
    _s("boards", "Boards", "/boards", "makes boards, adds and updates cards, and gives a workflow a board to work",
       ("create_board", "add_board_card", "update_board_card", "comment_on_card", "read_board", "list_boards",
        "give_board_access", "delete_board")),
    _s("chat", "Chat", "/", "talks to an agent for the person", ("chat_with_agent",)),
    _s("mcp_servers", "Apps", "/apps", "finds the app or MCP server for a service the person names, and connects it",
       ("prepare_connection", "find_in_catalog", "search_mcp_registry", "list_tool_servers", "list_server_tools")),
    _s("custom_tools", "Tools", "/tools", "makes HTTP and Python tools, imports an API's OpenAPI spec, tests a tool",
       ("create_http_tool", "create_python_tool", "import_openapi", "test_python_tool", "list_custom_tools",
        "delete_custom_tool")),
    _s("messaging", "Channels", "/channels", "says which chat channels are connected and which agents answer",
       ("read_workspace:channels",), cant="connecting a chat channel"),
    _s("phone", "Phone", "/channels/phone", "says which phone numbers answer and which agent", ("read_workspace:phone",),
       cant="getting or setting up a phone number"),
    _s("widgets", "Widgets", "/widgets", "says which website widgets are on and which agent answers",
       ("read_workspace:widgets",), cant="making or changing a website widget"),
    # Settings
    _s("general", "General", "/settings/general", "turns Boards or Knowledge on, and says the tool policy",
       ("turn_on_feature", "read_workspace:tool_policy"), cant="the workspace's other general settings"),
    _s("models", "Models", "/settings/models", "says the default and builder models and which providers have a key",
       ("read_workspace:models",), cant="adding a provider key or changing the default model"),
    _s("personal_details", "Personal details", "/settings/personal_details", cant="the person's own details"),
    _s("variables", "Variables", "/settings/variables",
       "names the workspace's variables for a step's `{{ global.x }}`, never a value",
       ("list_variables", "read_workspace:variables"), cant="adding or changing a variable"),
    _s("memory", "Memory", "/settings/memory", cant="seeing or clearing what agents remember"),
    _s("workspace", "Integrations", "/settings/workspace", cant="connecting the workspace's integrations"),
    _s("repos", "Repos", "/settings/repos", "lists the repositories agents may use", ("list_repos",),
       cant="adding a repository"),
    _s("db_configs", "DB configs", "/settings/db_configs", "lists the database connections", ("list_db_configs",),
       cant="adding a database connection"),
    _s("api_keys", "API keys", "/settings/api_keys", cant="making or revoking API keys"),
    _s("mcp_server", "MCP server", "/settings/mcp_server", cant="turning on this workspace's own MCP server"),
    _s("import_export", "Import / Export", "/settings/import_export", cant="importing or exporting the workspace"),
    _s("scale", "Scale", "/settings/scale", cant="the settings for running at scale"),
    _s("support", "Help & docs", "/settings/support", "answers how-to questions from the app's own help",
       ("search_help",)),
    _s("datalab", "Data lab", "/settings/datalab", cant="the Data lab"),
)

#: What the product does not do at all, on any screen, with the nearest thing that does (BWA1, D274): kept as written,
#: since no screen says it. A host keeps its own.
SELF_HOSTED_LIMITS = """\
What it does not do, and the nearest thing that does:
- It does not build native mobile or desktop apps. Nearest: an agent on the website widget or a chat channel, or a
  workflow behind an API.
- It does not place a phone call on request. Its phone line answers incoming calls. Nearest: a message on a chat
  channel or an email.
- It does not drive printers or other devices, or reach into someone else's computer. Nearest: prepare the document
  and send it (email, chat) for the person to print. A self-hosted install can read the folders on this machine that
  Settings allows.
- It cannot guarantee what others decide (search rankings, prices, approvals). Nearest: draft, research, watch and
  report.
- It never takes card numbers or passwords in chat, and does not store card data. Payments and bookings go through
  the provider's own checkout or a payment link; a service connects through its sign-in or a key on a secure screen.
- It cannot use a service that offers no API or app it can connect to (some booking systems are partner-only).
  Nearest: a workflow that researches the options and asks the person to book.
- Runs are not free: they cost what the models they use charge. Nearest: caps per run, smaller models for simple
  steps.
"""


_census: tuple[Screen, ...] | None = None


def set_census(census: tuple[Screen, ...] | None) -> None:
    """A host's own screens (cloud's census): what `current` gives the planner's pack."""
    global _census
    _census = census


def current() -> tuple[Screen, ...]:
    return _census or CENSUS


def _where(s: Screen) -> str:
    return f"{s.label} ({s.href})"


def not_here(census: tuple[Screen, ...] | None = None) -> list[dict[str, str]]:
    """What is not done from Synapse AI yet, each with its screen (the host's census unless given): the planner's
    pack reads it."""
    return [{"what": s.cant, "open": _where(s)} for s in (census or current()) if s.cant]


def capabilities_text(census: tuple[Screen, ...] = CENSUS, limits: str = SELF_HOSTED_LIMITS) -> str:
    """What this product does and does not do, from the census and the host's general limits."""
    does = "\n".join(f"- {s.label}: {s.does}." for s in census if s.tools)
    cant = "\n".join(f"- {s.cant[0].upper()}{s.cant[1:]}: open {_where(s)}." for s in census if s.cant)
    return (f"What this product does, and what you (Synapse AI) can do on each screen:\n{does}\n\n"
            "Not done from here yet: say so, name the screen, and never build a workflow that imitates it:\n"
            f"{cant}\n\n{limits}")
