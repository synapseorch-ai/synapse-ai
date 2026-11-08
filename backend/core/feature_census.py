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
    #: What a person calls this screen or what is on it, beyond its label ("test cases", "golden set"), lower case:
    #: GUIDE1's pack finds the screen by them (F1257). Each is this row's alone, and never another row's id or label.
    names: tuple[str, ...] = ()
    #: What a workflow built to imitate this screen looks like, so the planner can say plainly why it does not build
    #: one (F1257). A screen Synapse AI comes to reach keeps it: then it reads "use the real tools, don't rebuild it".
    imitation: str = ""
    #: What every plan is made of (workflows, agents, tools, and the runs and logs that show them working): GUIDE1's
    #: pack leaves these out of its `features` (F1257), which name what a request might mean beyond a plan's parts.
    plan_part: bool = False


def _s(id: str, label: str, href: str, does: str = "", tools: tuple[str, ...] = (), cant: str = "", *,
       names: tuple[str, ...] = (), imitation: str = "", plan_part: bool = False) -> Screen:
    return Screen(id, label, href, does, tools, cant, names, imitation, plan_part)


#: The self-hosted app's screens (nav.ts's HUB_NAV, then SETTINGS_NAV), in the rail's order.
CENSUS: tuple[Screen, ...] = (
    _s("dashboard", "Home", "/dashboard", "says what happened today and what waits on the person",
       ("read_workspace:today", "read_workspace:waiting", "summarise_activity", "list_waiting"),
       names=("dashboard", "overview")),
    _s("runs", "Runs", "/runs", "lists runs, reads one step by step, answers a run's question, stops one, says why "
       "one failed and resumes it once fixed",
       ("list_runs", "get_run", "read_workspace:run", "answer_run", "cancel_run", "resume_run"),
       names=("run history", "executions", "failed run"), plan_part=True),
    _s("logs", "Logs", "/logs", "reads what a run or an agent did, and why a run failed: its step, its error, the "
       "class of failure and the change that fixes it",
       ("read_workspace:run", "read_workspace:agent_activity", "agent_activity"), names=("activity log",),
       plan_part=True),
    _s("evals", "Evals", "/evals",
       cant="writing a rubric, making golden test cases, running an eval suite and reading eval scores",
       names=("eval", "evaluation", "evaluations", "eval suite", "test cases", "golden set", "rubric", "eval score"),
       imitation="a workflow whose agent grades another workflow's output against criteria and writes a score"),
    _s("usage", "Usage", "/usage", "says what workflows and models have cost, what the plan has left, and where it "
       "could cost less, each saving with its estimate as a change to make",
       ("get_usage", "read_workspace:usage", "read_workspace:plan", "read_workspace:savings"),
       names=("spend", "spending", "model costs")),
    _s("orchestrations", "Workflows", "/workflows", "builds, changes, checks, runs and deletes workflows",
       ("create_orchestration", "update_orchestration", "add_single_step", "update_step", "remove_step",
        "validate_orchestration", "run_orchestration", "delete_orchestration", "read_workspace:workflow"),
       names=("orchestration", "pipeline"), plan_part=True),
    _s("schedules", "Schedules", "/schedules", "makes, changes, turns on, pauses, runs now and deletes schedules, sets "
       "what one does with a time it missed and points one at another workflow or agent",
       ("create_schedule", "update_schedule", "set_schedule_enabled", "turn_on_schedule", "pause_schedule",
        "trigger_schedule", "delete_schedule"),
       names=("cron", "cron job", "timer")),
    _s("templates", "Templates", "/templates", cant="browsing and importing the ready-made template packs",
       names=("template", "template pack", "starter workflows"),
       imitation="a workflow rebuilt by hand to copy one of the ready-made templates"),
    _s("agents", "Agents", "/agents", "makes, changes and deletes agents, talks to one, and says what one has done",
       ("create_agent", "update_agent", "delete_agent", "chat_with_agent", "read_workspace:agent_activity"),
       names=("chatbot",), plan_part=True),
    _s("skills", "Skills", "/skills", "writes skills, gives them to agents and deletes them",
       ("create_skill", "update_skill", "attach_skills", "list_skills", "delete_skill"),
       cant="installing a skill from the Skills registry, which a person does on that screen", names=("skill",)),
    _s("browser", "Browser", "/browser", "lists the kept sign-ins and recorded tasks, uses or drops a fix a run kept "
       "on a recording, renames and deletes a recording",
       ("list_browser_sessions", "list_recordings", "get_recording", "delete_recording", "approve_recording_edit",
        "discard_recording_edit", "rename_recording"),
       cant="signing in to a site or recording a task, which a person does in the app's browser",
       names=("recording", "recorded task", "kept sign-in"),
       imitation="a workflow whose agent drives a browser to sign in or record a task in the person's place"),
    _s("vault", "Vault", "/vault", cant="browsing, uploading and organising the Vault's files",
       names=("vault file", "file storage")),
    _s("knowledge", "Knowledge", "/knowledge", "makes knowledge bases, adds files, pages and notes, and searches them",
       ("create_knowledge_base", "add_knowledge_file", "add_knowledge_pages", "add_knowledge_note",
        "search_knowledge", "update_knowledge_base", "delete_knowledge_base", "list_knowledge_documents"),
       names=("knowledge base", "rag", "knowledge note", "knowledge documents")),
    _s("boards", "Boards", "/boards", "makes boards, renames and describes them, adds and updates cards and their "
       "words, and says which agents, workflows and schedules may use one",
       ("create_board", "add_board_card", "update_board_card", "comment_on_card", "read_board", "list_boards",
        "give_board_access", "delete_board", "update_board"),
       names=("board", "kanban", "board card")),
    _s("chat", "Chat", "/", "talks to an agent for the person", ("chat_with_agent",), names=("conversation",)),
    _s("mcp_servers", "Apps", "/apps", "finds the app or MCP server for a service the person names, and connects it",
       ("prepare_connection", "find_in_catalog", "search_mcp_registry", "list_tool_servers", "list_server_tools"),
       names=("app catalogue", "connectors", "mcp", "tool servers")),
    _s("custom_tools", "Tools", "/tools", "makes HTTP and Python tools, imports an API's OpenAPI spec, tests a tool",
       ("create_http_tool", "update_http_tool", "create_python_tool", "update_python_tool", "import_openapi",
        "test_python_tool", "test_code_change", "list_custom_tools", "delete_custom_tool"),
       names=("http tool", "python tool", "openapi", "api spec"), plan_part=True),
    _s("messaging", "Channels", "/channels", "connects a Telegram, Discord, Slack, Teams or WhatsApp channel on a "
       "Connect card (its token typed there), turns one on or off, removes it, sends one test message, says which "
       "agents answer, and sends run notices there",
       ("read_workspace:channels", "prepare_connection", "set_channel_enabled", "delete_channel", "test_channel",
        "set_channel_agents", "set_run_notification_channel"),
       names=("channel", "telegram", "slack", "whatsapp", "discord"),
       imitation="a workflow that polls a chat service's API on a schedule in place of a connected channel"),
    _s("phone", "Phone", "/channels/phone", "says which phone numbers answer and which agent", ("read_workspace:phone",),
       cant="getting or setting up a phone number", names=("phone number", "phone call", "voice"),
       imitation="a workflow that calls a telephony API to stand in for a phone number"),
    _s("widgets", "Widgets", "/widgets", "makes, changes, turns off and deletes website widgets (their agent, sites, "
       "look, the tools a visitor may use, daily cap), gives one a new key or a logo, and says which are on",
       ("read_workspace:widgets", "create_widget", "update_widget", "delete_widget", "rotate_widget_key",
        "set_widget_logo"),
       names=("widget", "chat widget", "website widget", "embed"),
       imitation="an agent or workflow built to stand in for a website widget, with no snippet to paste"),
    # Settings
    _s("general", "General", "/settings/general", "changes the general settings on Synapse AI's list (compaction, "
       "caching, large responses, turn limits, the tool policy, features and this install's limits), each with the "
       "person's yes and undo",
       ("turn_on_feature", "set_settings", "read_workspace:settings", "read_workspace:tool_policy"),
       cant="the settings off its list: how agents reach this machine and its databases, signing in, and running at "
            "scale"),
    _s("models", "Models", "/settings/models", "says which providers have a key, and changes the default, builder, "
       "quick and judge models, reasoning effort and the longest answer, with the person's yes and undo",
       ("read_workspace:models", "set_settings", "read_workspace:settings"),
       cant="adding a provider key or a provider's address"),
    _s("personal_details", "Personal details", "/settings/personal_details", cant="the person's own details"),
    _s("variables", "Variables", "/settings/variables",
       "names the workspace's variables for a step's `{{ global.x }}`, never a value",
       ("list_variables", "read_workspace:variables"), cant="adding or changing a variable",
       names=("variable", "env vars")),
    _s("memory", "Memory", "/settings/memory", cant="seeing or clearing what agents remember",
       names=("agent memory", "remembers")),
    _s("workspace", "Integrations", "/settings/workspace", cant="connecting the workspace's integrations"),
    _s("repos", "Repos", "/settings/repos", "lists the repositories agents may use", ("list_repos",),
       cant="adding a repository"),
    _s("db_configs", "DB configs", "/settings/db_configs", "lists the database connections", ("list_db_configs",),
       cant="adding a database connection"),
    _s("api_keys", "API keys", "/settings/api_keys", cant="making or revoking API keys", names=("api key", "api token"),
       imitation="a tool or workflow that makes up a key, or a key written into chat"),
    _s("mcp_server", "MCP server", "/settings/mcp_server", cant="turning on this workspace's own MCP server"),
    _s("import_export", "Import / Export", "/settings/import_export",
       cant="importing or exporting the workspace", names=("import", "export", "backup")),
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


#: Who Synapse AI is (F1382): ahead of every host's capabilities text (`builder_help.capabilities()`), so it is read on
#: every route. Descriptive only: the rules for what the chat says are its steps' own, and the planner, which reads
#: this too, still researches.
WHO_YOU_ARE = """\
Who you are: Synapse AI, the assistant inside this product. You explain the product, read and change what this
workspace has, and build the workflows and agents that do its work: plain steps (a tool, a model call) where that
does the job, an agent where people ask it things as they come or the work needs judgement, run once now or on a
schedule.
"""

#: What the product delivers, one plain example each (F1382): the answering steps' alone (`capabilities(examples=True)`).
#: The planner never reads it, so no example becomes a step its plan did not ask for (CAP3: most of them send).
WHAT_IT_DELIVERS = """\
What the product delivers, with an example of each:
- Work that runs on its own on a schedule: a summary of yesterday's new sign-ups, sent each morning by email.
- Agents that answer for the business on its website or a chat channel: questions answered from its own documents.
- Workflows started by an event: a new form entry checked, saved to a sheet, and the team told on Slack.
- Reports from the apps and data it connects to: a weekly summary of new deals read from a connected CRM.
- What changes outside, fetched and sent: an exchange rate or a flight's status, each morning to Telegram.
- A person in the loop: any step can wait for someone's approval before it acts.
"""


_census: tuple[Screen, ...] | None = None


def set_census(census: tuple[Screen, ...] | None) -> None:
    """A host's own screens (cloud's census): what `current` gives the planner's pack."""
    global _census
    _census = census


def current() -> tuple[Screen, ...]:
    return _census or CENSUS


def link(s: Screen) -> str:
    """The screen as a link the person can follow, `[Evals](/evals)`: the host's row, so cloud's own path (F1265)."""
    return f"[{s.label}]({s.href})"


def not_here(census: tuple[Screen, ...] | None = None) -> list[dict[str, str]]:
    """What is not done from Synapse AI yet, each with its screen (the host's census unless given) and the row's id,
    which a `cannot` names as its `screen`: the planner's pack reads it."""
    return [{"what": s.cant, "open": link(s), "screen": s.id} for s in (census or current()) if s.cant]


def capabilities_text(census: tuple[Screen, ...] = CENSUS, limits: str = SELF_HOSTED_LIMITS) -> str:
    """What this product does and does not do, from the census and the host's general limits."""
    does = "\n".join(f"- {s.label}: {s.does}." for s in census if s.tools)
    cant = "\n".join(f"- {s.cant[0].upper()}{s.cant[1:]}: open {link(s)} (screen `{s.id}`)." for s in census if s.cant)
    return (f"What this product does, and what you (Synapse AI) can do on each screen:\n{does}\n\n"
            "Not done from here yet: say so, name the screen (`cannot`'s `screen` is its id), and never build a "
            "workflow that imitates it:\n"
            f"{cant}\n\n{limits}")
