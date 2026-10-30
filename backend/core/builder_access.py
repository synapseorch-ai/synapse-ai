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
* a run that names no person (anything but the assistant's) is not touched.

The person comes from the run's `_caller` state key, which only the host writes when it starts the run: a run's
inputs may not set a key that starts with an underscore (D126).
"""
from __future__ import annotations

import contextlib
import contextvars
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
})

#: They show the person a card (Connect, the findings, an action to press): nothing changes until the person acts,
#: and then through the app's own doors under their own role.
#: The change route's finish tools and its check (SAI2-P2) write only the run's own state, or put up an act's card;
#: so do the build route's (SAI2-P3).
#: `read_turn`, `classify_request` (and `classify_at_plan`, while a plan waits) and `answer_pack` (SAI2-P4) read the
#: conversation's last turn, type this one and build the answer's context: they write the run's own state and the
#: turn's card, and put up a pick's act card or a named provider's Connect card.
CARDS: frozenset[str] = frozenset({"prepare_connection", "report_findings", "prepare_action", "submit_change",
                                   "ask_person", "cannot", "check_change", "check_made", "submit_plan", "check_build",
                                   "submit_prompts", "read_turn", "classify_request", "classify_at_plan", "answer_pack",
                                   "effort_check", "finish_findings"})

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
})

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


def forget_runs() -> None:
    _PEOPLE.clear()


def current_caller() -> Caller | None:
    """The person the builder tool being called works for, inside a builder run that names one."""
    return _current.get()


async def _person(run_id: str | None):
    if not run_id:
        return _NOBODY
    if run_id in _PEOPLE:
        return _PEOPLE[run_id]
    from core.orchestration.state import SharedState

    try:
        state = (await SharedState.restore(run_id)).run.shared_state or {}
    except FileNotFoundError:
        return _NOBODY  # not checkpointed yet: no host-written person to go by
    entry = state.get(CALLER_KEY)
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


async def refusal(tool_name: str, run_id: str | None) -> tuple[str | None, Caller | None]:
    """(why `tool_name` may not run in this run, or None; the run's person, if it names one)."""
    person = await _person(run_id)
    if person is _NOBODY:
        return None, None
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
