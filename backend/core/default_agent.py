"""
The agent a new install starts with: Synapse AI, the product's assistant (ASSIST1, D291).

Naveen: "let our builder be the default agent". It is a builder-typed agent, so Chat talks to it through the
assistant's own conversation (`run_builder_stream_compat` for an API caller): what it reads, its cards, its plan
reviews. It replaced an all-tools chat agent of the same name and id (`LEGACY_DEFAULT_AGENT`).

Created once by the installer (`synapse setup --apply`), never overwritten after: a user who edits or deletes it
keeps their choice. An install whose old default is exactly as the installer made it has it turned into the assistant
once, at start-up (`adopt_the_assistant`), under the same id, so its conversations stay; an edited one is the
person's own and is left alone. Its id is also how the legacy importer tells it apart from the user's own agents — a
store holding only this agent and the seeded builder is still an empty one.
"""
from __future__ import annotations

DEFAULT_AGENT_ID = "agent_synapse_ai"

#: The default agent before ASSIST1, exactly as the installer made it: an install still holding it unchanged has it
#: turned into the assistant (`adopt_the_assistant`).
LEGACY_DEFAULT_AGENT = {
    "id": DEFAULT_AGENT_ID,
    "name": "Synapse AI",
    "description": "Your all-purpose AI assistant with access to every capability -- browsing, code execution, file management, and more.",
    "avatar": "default",
    "type": "conversational",
    "tools": ["all"],
    "repos": [],
    "system_prompt": (
        "# Role & Identity\n"
        "You are Synapse AI -- an elite, all-purpose AI assistant with access to the full suite of tools on this platform. "
        "You exist to help the user accomplish any task with speed, accuracy, and clarity.\n\n"
        "# Core Capabilities\n"
        "You can browse the web and extract real information from any source.\n"
        "You can read, write, and manage files and directories on the local filesystem.\n"
        "You can execute Python code for calculations, data processing, and automation.\n"
        "You can interact with APIs, databases, and external services via configured tools.\n"
        "You can store and retrieve information between sessions using vault tools.\n"
        "You understand images, PDFs, spreadsheets, and structured data.\n\n"
        "# Approach & Methodology\n"
        "**Think before acting:** Understand the full request before choosing tools.\n"
        "**Use tools over memory:** Always fetch real data with tools -- never fabricate information.\n"
        "**Be concise and direct:** Give the most useful answer with minimal fluff.\n"
        "**Confirm and verify:** When you take an action (write a file, run code, browse a site), confirm what was done.\n"
        "**Adapt to complexity:** Short answers for simple questions; structured, detailed responses for complex tasks.\n\n"
        "# Output Style\n"
        "Use Markdown for structured outputs -- tables, lists, and code blocks where appropriate.\n"
        "For multi-step tasks, briefly outline what you're doing before you do it.\n"
        "When something fails or is uncertain, explain clearly and suggest next steps.\n\n"
        "# Constraints\n"
        "Never fabricate data, file contents, statistics, or API responses -- use tools.\n"
        "Never expose sensitive information (API keys, passwords) in responses.\n"
        "Always respect the user's filesystem and data -- ask before destructive operations."
    ),
}


DEFAULT_AGENT = {
    "id": DEFAULT_AGENT_ID,
    "name": "Synapse AI",
    "description": "The product's assistant: ask about your workspace, have it run or answer things with a card you "
                   "press, or describe a job and it builds the workflow.",
    "avatar": "default",
    "type": "builder",
    "tools": [],
    "repos": [],
    "system_prompt": "",
}

#: What decides that a stored default is still the installer's own: an edit to any of these makes it the person's.
_AS_INSTALLED = ("name", "description", "type", "tools", "repos", "system_prompt", "model", "provider")


async def adopt_the_assistant() -> bool:
    """Turn an untouched old default agent into the assistant, once. True when it was turned."""
    from core.store.resources import get_agent, save_agent

    stored = await get_agent(DEFAULT_AGENT_ID)
    if not stored or stored.get("type") == "builder":
        return False
    if any(stored.get(k) != LEGACY_DEFAULT_AGENT.get(k) for k in _AS_INSTALLED):
        return False
    await save_agent({**stored, **DEFAULT_AGENT})
    return True


async def ensure_default_agent() -> bool:
    """Create the default agent unless it exists. True when it was created."""
    from core.store.resources import get_agent, save_agent

    if await get_agent(DEFAULT_AGENT_ID):
        return False
    await save_agent(dict(DEFAULT_AGENT))
    return True
