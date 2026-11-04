"""Whom a builder run works for, and what that person may have it do (ASSIST1, D291, F681).

Every role may talk to the assistant (Naveen, D291): a viewer reads, an approver reads and answers, a builder and
above also builds and changes. The assistant's run asks its questions through the run's human-input door, which an
approver may answer — including the review of a change plan. So the line is drawn here, in code, where every builder
tool call passes (`builder_tools.execute_builder_tool_with_events`), and nowhere in a prompt:

* every builder tool is a **read**, a **card** (it puts something in front of the person to click and changes
  nothing itself) or a **write**, and `test_the_builder_does_only_what_its_person_may.py` fails on a tool that is
  none of them;
* a write is refused when the run's person is below builder by their role **now**, read on every call through the
  host's role source (`product_tools.hooks`), so a demotion while a run waits for its review takes effect;
* a builder tool is **usable anywhere** (the primitives a person may build with, which MCP serves too, and plain
  reads) or **Synapse AI's own run only** (its cards and the steps that act on a yes kept in its run's state), and a
  tool nobody classified is its own run's: an ordinary agent or workflow that names one is refused (F1315);
* a run that is not Synapse AI's own names no person, and is otherwise not touched.

The person comes from the `_caller` state key of Synapse AI's own run, which only the host writes when it starts the
run: a run's inputs may not set a key that starts with an underscore (D126), and no step may write one (F1315). In
any other run the key names no one, whatever wrote it.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Awaitable, Callable, Iterator

from core.product_tools import hooks
from core.product_tools.caller import Caller
from core.product_tools.roles import Role

#: The run state key the host writes when it starts the assistant's run: `{"user_id": … | None}`.
CALLER_KEY = "_caller"

READS: frozenset[str] = frozenset({
    "list_agents", "get_agent", "list_all_tools", "get_tools_detail", "list_tool_servers", "list_server_tools",
    "list_repos", "list_db_configs", "list_orchestrations", "get_orchestration", "validate_orchestration",
    "list_custom_tools", "list_skills", "list_knowledge_bases", "get_skill", "list_schedules", "list_variables",
    "list_recent_runs", "get_run_summary", "read_workspace", "search_help", "product_capabilities", "check_plan",
    "find_in_catalog", "search_mcp_registry", "search_the_web", "read_web_page", "read_change_context",
    "read_plan_context", "read_plan_answer", "look_up_providers",
    # The planner's read of the person's own API spec (F1225): it fetches the spec, and changes nothing.
    "read_api_spec",
    # An answer's own check (F1243): it reads the run's answer and changes nothing.
    "check_answer",
    # The planner's research answered (D340 D2): code reads the help and a page the question names through the
    # guarded read, and keeps the researcher's answers, into the run's own state only.
    "answer_research", "settle_research",
    # The knowledge bases' passages, by document (F1258): a search, nothing changed.
    "search_knowledge",
    # The planner's own check of a draft (D340 D3): examine's rules, written nowhere.
    "check_draft",
})

#: They show the person a card (Connect, the findings, an action to press): nothing changes until the person acts,
#: and then through the app's own doors under their own role.
#: The change route's finish tools and its check (SAI2-P2) write only the run's own state, or put up an act's card;
#: so do the build route's (SAI2-P3), the one planner's `say` (D340 D0: a reply, into the run's own state), and its
#: `research_questions` and their one-form ask, `ask_research` (D340 D2: the questions, into the run's own state).
#: `read_turn`, `classify_request` (and `classify_at_plan`, while a plan waits) and `answer_pack` (SAI2-P4) read the
#: conversation's last turn, type this one and build the answer's context: they write the run's own state and the
#: turn's card, and put up a pick's act card or a named provider's Connect card.
CARDS: frozenset[str] = frozenset({"prepare_connection", "report_findings", "prepare_action", "submit_change",
                                   "ask_person", "cannot", "check_change", "check_made", "submit_plan", "check_build",
                                   "submit_prompts", "read_turn", "classify_request", "classify_at_plan", "answer_pack",
                                   "effort_check", "finish_findings", "say", "research_questions",
                                   "ask_research"})

WRITES: frozenset[str] = frozenset({
    "deploy_orchestration", "create_agent", "create_agents", "update_agent", "create_orchestration",
    "update_orchestration", "create_orchestration_skeleton", "add_steps", "set_orchestration_meta", "update_step",
    "remove_step", "add_single_step", "add_multiple_steps", "create_http_tool", "create_python_tool", "test_python_tool", "import_openapi",
    "delete_custom_tool", "create_skill", "update_skill", "attach_skills", "finish_build", "create_schedule",
    "set_schedule_enabled", "update_schedule", "delete_schedule", "delete_agent", "delete_orchestration",
    "approve_change", "apply_change", "approve_plan", "save_build",
    # Reads the answer at a build's wait for a connection, and removes the tools that build made on "stop" or "build
    # without" (F946): a write, of the build's own unsaved tools only.
    "read_connect_answer",
    # The settings a reviewed change sets (F1264's settings_set): a write here; the tool itself still asks an admin.
    "set_settings",
    # BRW1's reviewed changes (F1258, F1276, F1259): a knowledge note's text, a base's settings, an agent's memory item.
    "edit_knowledge_document", "update_knowledge_base", "edit_agent_memory_item", "delete_agent_memory_item",
    # A skill a reviewed change deletes (F1266's delete_skill): a write, for the builder role, as its create and update.
    "delete_skill",
})

#: Usable anywhere (F1315): an ordinary agent or workflow may name these. The primitives a person may build with, which
#: MCP serves too (`product_tools.build`), and reads of the workspace, the help and the web.
ANYWHERE: frozenset[str] = frozenset({
    "list_agents", "get_agent", "list_orchestrations", "get_orchestration", "validate_orchestration",
    "list_tool_servers", "list_server_tools", "list_all_tools", "get_tools_detail", "list_custom_tools", "list_skills",
    "get_skill", "list_schedules", "list_repos", "list_db_configs", "list_knowledge_bases",
    "create_agent", "create_agents", "update_agent", "create_orchestration", "create_orchestration_skeleton",
    "add_steps", "add_single_step", "add_multiple_steps", "update_step", "remove_step", "update_orchestration",
    "set_orchestration_meta", "deploy_orchestration", "create_http_tool", "create_python_tool", "test_python_tool",
    "import_openapi", "create_skill", "update_skill", "attach_skills", "create_schedule", "update_schedule",
    "set_schedule_enabled", "delete_agent", "delete_orchestration", "delete_schedule", "delete_custom_tool",
    "list_variables", "list_recent_runs", "get_run_summary", "read_workspace", "search_help", "product_capabilities",
    "find_in_catalog", "search_mcp_registry", "search_the_web", "read_web_page", "look_up_providers", "read_api_spec",
    "search_knowledge",
})

#: Synapse AI's own run only (F1315): its cards, which put something in front of its person; the steps that act on a
#: yes kept in its run's state, which an ordinary workflow could write itself; the reads of its own run's plan and
#: research. Every builder tool is in this list or `ANYWHERE`
#: (`test_synapse_ai_own_steps_stay_in_its_own_run.py`), and one in neither is refused as this list's.
OWN_RUN_ONLY: frozenset[str] = CARDS | frozenset({
    "read_change_context", "read_plan_context", "read_plan_answer", "check_plan", "check_answer", "check_draft",
    "answer_research", "settle_research",
    "approve_change", "apply_change", "approve_plan", "save_build", "finish_build", "read_connect_answer",
})


def run_only(tool: str) -> str:
    """What an ordinary agent or workflow is told when it calls one of Synapse AI's own steps (F1315)."""
    return (f"Not done: {tool} is one of Synapse AI's own steps, which only its own run takes. An agent or a "
            "workflow's Tool step can't use it.")


def run_only_named(names: Any) -> str | None:
    """Why an agent's tools or a workflow's Tool steps that name Synapse AI's own steps may not be saved; None when
    they name none. A name that is no builder tool is not this check's."""
    from core.builder_tools import BUILDER_TOOL_NAMES

    own = sorted({n for n in names or () if isinstance(n, str) and n in BUILDER_TOOL_NAMES and n not in ANYWHERE})
    if not own:
        return None
    listed = ", ".join(f"'{n}'" for n in own)
    if len(own) == 1:
        return (f"{listed} is one of Synapse AI's own steps, which only its own run takes: an agent or a workflow's "
                "Tool step can't name it. Remove it.")
    return (f"{listed} are Synapse AI's own steps, which only its own run takes: an agent or a workflow's Tool step "
            "can't name them. Remove them.")


KINDS: dict[str, str] = {**{n: "read" for n in READS}, **{n: "card" for n in CARDS}, **{n: "write" for n in WRITES}}

#: Who builds and changes: a builder, an admin, an owner.
MAY_CHANGE = Role.BUILDER

_current: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("builder_access_caller", default=None)

#: A host's record of the person (cloud's `Principal`), for what its hooks read off `Caller.principal` (the audit,
#: what waits for them). `fn(user_id) -> record | None`; self-hosted has none.
PrincipalSource = Callable[[str | None], Awaitable[Any]]
_principal_source: PrincipalSource | None = None


def set_principal_source(fn: PrincipalSource | None) -> None:
    global _principal_source
    _principal_source = fn


async def _caller(role: Role, person: str | None) -> Caller:
    principal = await _principal_source(person) if _principal_source is not None else None
    return Caller(role=role, via="assistant", user_id=person, principal=principal)

#: run id → the person's id (None: the install's one person), for runs that name one. `_caller` never changes during
#: a run, so it is read from the checkpoint once; the role is read again on every call.
_PEOPLE: dict[str, str | None] = {}
_NOBODY = object()
_MAX_RUNS = 2000

_log = logging.getLogger(__name__)

#: What a builder run that names no person is told (F1315): it acts for no one, so it does nothing.
NO_ONE = ("Not done: this run of Synapse AI names no person to act for, so it does nothing. Ask Synapse AI in "
          "the app.")


def forget_runs() -> None:
    _PEOPLE.clear()


def acting_caller() -> Caller:
    """Whom a builder tool acts for: the person of Synapse AI's own run; else the client the product's registry
    checked and bound (an MCP call through `product_tools.build`); else the owner, as for every tool usable anywhere.
    Synapse AI's own run that names no one never gets here (`refusal`, F1315), and nor does one of its own steps called
    from any other run."""
    from core.product_tools import caller as bound

    return _current.get() or bound.current() or Caller(role=Role.OWNER, via="assistant")


def current_caller() -> Caller | None:
    """The person the builder tool being called works for, inside a builder run that names one."""
    return _current.get()


async def _run_of(run_id: str | None) -> Any:
    """The run, live in this process or as last checkpointed; None when it is neither."""
    if not run_id:
        return None
    from core.orchestration.engine import live_run
    from core.orchestration.state import SharedState

    # The live run first: a run this process works under holds its person before its first checkpoint (F1315).
    run = live_run(run_id)
    if run is not None:
        return run
    try:
        return (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        return None


async def _own_run(run: Any) -> bool:
    from core.native_builder.identity import is_builder_run

    return run is not None and await is_builder_run(run)


async def _person(run_id: str | None, run: Any = None):
    """The person Synapse AI's own run works for, from the key only its host writes. Any other run names no one: its
    steps could have written that key themselves (F1315)."""
    if not run_id:
        return _NOBODY
    if run_id in _PEOPLE:
        return _PEOPLE[run_id]
    run = run if run is not None else await _run_of(run_id)
    if not await _own_run(run):
        return _NOBODY
    entry = (run.shared_state or {}).get(CALLER_KEY)
    if not isinstance(entry, dict):
        return _NOBODY
    if len(_PEOPLE) >= _MAX_RUNS:
        _PEOPLE.clear()
    _PEOPLE[run_id] = entry.get("user_id") or None
    return _PEOPLE[run_id]


async def _role(user_id: str | None) -> Role | None:
    """The person's role now; None when they are no longer in the workspace. A host without roles (self-hosted)
    has one person, the owner."""
    try:
        return await hooks.role_of(user_id or "")
    except LookupError:
        return None


async def caller_for(run_id: str | None) -> Caller | None:
    person = await _person(run_id)
    if person is _NOBODY:
        return None
    role = await _role(person)
    return await _caller(role, person) if role is not None else None


async def refusal(tool_name: str, run_id: str | None, *, door: str | None = None
                  ) -> tuple[str | None, Caller | None]:
    """(why `tool_name` may not run in this run, or None; the run's person, if it names one).

    `door`: how an ordinary agent or workflow reached the tool, `"agent"` (its tools) or `"step"` (a Tool step); None
    for the install's own direct call."""
    run = await _run_of(run_id)
    if not await _own_run(run):
        # Not Synapse AI's own run: it names no one, and only a tool usable anywhere is its to call (F1315).
        if door is not None and tool_name not in ANYWHERE:
            _log.warning("refused %s, one of Synapse AI's own steps, at an ordinary run's %s (run %s)",
                         tool_name, door, run_id)
            return run_only(tool_name), None
        return None, None
    person = await _person(run_id, run)
    if person is _NOBODY:
        # Synapse AI's own run acts for the person who started it: never for whoever a fallback would be (F1315,
        # fails closed), where people are told apart. Self-hosted has one person, whose run it is: one paused before
        # its person was recorded (an older release's) is theirs.
        if hooks.tells_people_apart():
            return NO_ONE, None
        person = None
    role = await _role(person)
    if role is None:
        return ("Not done: you are no longer a member of this workspace, so I can't act for you here."), None
    caller = await _caller(role, person)
    kind = KINDS.get(tool_name, "write")  # a tool nobody classified is treated as the riskiest kind
    if kind == "write" and not role.can_act_as(MAY_CHANGE):
        return (f"Not done: building and changing things needs the {MAY_CHANGE.label} role, and you are a "
                f"{role.label} in this workspace. Tell the person what you would have changed, and that someone "
                f"with the {MAY_CHANGE.label} role can make the change."), caller
    return None, caller


@contextlib.contextmanager
def acting_for(caller: Caller | None) -> Iterator[None]:
    token = _current.set(caller)
    try:
        yield
    finally:
        _current.reset(token)
