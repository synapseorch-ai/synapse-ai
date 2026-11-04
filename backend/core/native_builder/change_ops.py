"""Synapse AI's change ops: one closed table (SAI2-P2, F792, D308).

A change is a list of ops, each `{op, target: {kind, id | name}, fields}` (`objects.ChangeOp`). The change planner
(a model) proposes them; everything else is a fact that code decides from this table:

* **what an op may set**: its `fields`, and which of them it needs. A field it does not take is refused by name, as
  the write tools refuse theirs (F784's rule: a write never takes an id from its payload, and never drops a key
  silently);
* **what it calls**: one registry tool for a write (`registry.invoke`, so the audit is by id, F697); `prepare_action`'s
  card for an act (`ACTIONS`, F755: the op's kind decides card or review, never text);
* **what the target must look like afterwards** (`expected`), which is both the no-op check before and the effect
  check after (`differs`);
* **what undoes it** (`inverse`), from the target as it was.

Three ops hand the request to another route instead: `edit_steps` (a workflow's steps: the build route's update
path, on the typed Plan since P3), `create` (a new agent, skill, HTTP tool or OpenAPI import: today's create
path, which holds those tools' checks — F769's host rule, TAG1's method defaults, the Connect card, F489; Naveen's
split, 10-22; an HTTP tool whose url the planner has is `create_tool` instead, made by code in the change itself, F1322)
and `use_recording` (running, scheduling or repeating a recording: the build route makes the workflow
whose Browser step replays it, F1231). None is a second way to make those things.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from core import settings_allow

Kind = Literal["act", "write", "delete", "create", "handoff"]

#: Every kind of thing an op can be about.
TARGET_KINDS = ("workflow", "agent", "schedule", "tool", "skill", "board", "knowledge_base", "document", "run",
                "approval", "card", "openapi", "python_tool", "feature", "recording", "settings", "memory", "widget",
                "channel", "dead_job", "support")

#: Tool-policy decisions an op may set; `inherit` removes the agent's own rule, so the workspace's applies.
DECISIONS = ("allow", "confirm", "deny", "inherit")
POLICY_CATEGORIES = ("destructive", "open_world", "undeclared")


@dataclass(frozen=True)
class Op:
    name: str
    kind: Kind
    #: The target's kind (`create`'s is one of `CREATES`).
    target: str
    #: The registry tool a write calls; None for an act (its card's tool is `prepare_action`'s) and a hand-over.
    tool: str | None = None
    fields: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    #: At least one of `fields` must be set.
    any_of: bool = False
    #: The review's words for it: "<verb> <target>".
    verb: str = ""
    #: Built by code only, never offered to a model: an undo's re-create from a before-image (F797).
    internal: bool = False

    @property
    def is_act(self) -> bool:
        return self.kind == "act"

    @property
    def noun(self) -> str:
        """The target's kind as the person reads it: "knowledge base", never `knowledge_base`."""
        return self.target.replace("_", " ")

    @property
    def is_write(self) -> bool:
        return self.kind in ("write", "delete", "create")


#: The agent editor's plain settings a change sets (F1259): `set_agent_settings`'s fields.
AGENT_SETTINGS = ("description", "memory", "knowledge", "boards_own", "planning", "skip_default_tools", "helpers",
                  "max_turns", "turn_limit_mode", "reasoning_effort", "max_output_tokens")
#: The ops that set one section of the agent editor each (F1259).
AGENT_SECTIONS = ("set_agent_settings", "set_agent_team", "set_agent_browser", "set_agent_code")
#: A team's: members added or taken out, and how they work.
TEAM_SETTINGS = ("add", "remove", "mode", "at_once", "task_budget", "spend_limit_usd", "clear_spend_limit")
#: A browser agent's: where it may go, the sessions it starts with, how it works, its recordings and actions.
BROWSER_SETTINGS = ("sites", "sessions", "mode", "recordings", "action_budget")

#: What a new HTTP tool is made from (F1322): `create_http_tool`'s own facts, in a change's words. `key` says where its
#: key goes, {header, scheme}, never the key, which the person gives on its Connect card after the yes. `from` names
#: where code reads them (F1312a): {curl: n}, a curl the person pasted this turn, or {spec, operation}, an operation of
#: a spec the conversation read.
HTTP_TOOL_FIELDS = ("description", "method", "url", "headers", "body", "input_schema", "group", "key", "from")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
#: What a key may be sent after, as the Connect card saves it (`routes.tools.connect_made_tools`): or nothing.
KEY_SCHEMES = ("Bearer", "Token")

#: What a card sets on a website widget, in the card's words (`builder_actions._WIDGET_ARGS`).
_WIDGET_FIELDS = ("agent", "sites", "visitor_tools", "greeting", "title", "accent", "theme", "position",
                  "launcher_label", "font", "header_style", "launcher", "daily_cap")
#: A widget's look: each a closed list (`chat_widgets.branding`).
_WIDGET_CHOICES = {"theme": ("auto", "light", "dark"), "position": ("right", "left"),
                   "font": ("system", "sans", "serif", "rounded", "mono"), "header_style": ("accent", "neutral"),
                   "launcher": ("bubble", "logo")}

_ACTS = (
    Op("run_workflow", "act", "workflow", fields=("message",), verb="Run"),
    Op("run_schedule_now", "act", "schedule", verb="Run now"),
    Op("schedule_on", "act", "schedule", verb="Turn on"),
    Op("schedule_off", "act", "schedule", verb="Turn off"),
    Op("answer_run", "act", "run", verb="Answer"),
    Op("answer_tool_call", "act", "approval", verb="Answer"),
    Op("cancel_run", "act", "run", verb="Stop"),
    Op("resume_run", "act", "run", verb="Resume"),
    # Asking an agent for the person (F1274): a card, its answer shown once pressed.
    Op("talk_to_agent", "act", "agent", fields=("message",), required=("message",), verb="Ask"),
    # A hosted workspace's (F1260): a job the fleet gave up on, and a support request (named by its subject).
    Op("dead_job_retry", "act", "dead_job", verb="Retry"),
    Op("dead_job_discard", "act", "dead_job", verb="Discard"),
    Op("support_request", "act", "support", fields=("text", "category", "severity", "run_id"), required=("text",),
       verb="Send"),
    Op("knowledge_create", "act", "knowledge_base", fields=("text", "folder"), verb="Make"),
    Op("knowledge_add_file", "act", "knowledge_base", fields=("path",), required=("path",), verb="Add a file to"),
    Op("knowledge_add_pages", "act", "knowledge_base", fields=("urls", "kind"), required=("urls",),
       verb="Add pages to"),
    Op("knowledge_add_note", "act", "knowledge_base", fields=("title", "text"), required=("text",),
       verb="Add a note to"),
    Op("knowledge_retry_document", "act", "document", verb="Index again"),
    Op("knowledge_rename", "act", "knowledge_base", fields=("name", "text"), any_of=True, verb="Change"),
    Op("knowledge_rebuild", "act", "knowledge_base", verb="Rebuild"),
    Op("knowledge_delete_base", "act", "knowledge_base", verb="Delete"),
    Op("knowledge_delete_document", "act", "document", verb="Delete"),
    Op("board_add_card", "act", "board", fields=("title", "text"), required=("title",), verb="Add a card to"),
    Op("board_update_card", "act", "card", fields=("status", "result"), verb="Update"),
    Op("board_comment", "act", "card", fields=("text",), required=("text",), verb="Comment on"),
    Op("board_create", "act", "board", fields=("text",), verb="Make"),
    Op("board_delete", "act", "board", verb="Delete"),
    # A shared board's name, what it is for, and who may use it; a card's words (F1268): the Boards screen's service.
    Op("board_rename", "act", "board", fields=("name",), required=("name",), verb="Rename"),
    Op("board_describe", "act", "board", fields=("text",), required=("text",), verb="Describe"),
    Op("board_access", "act", "board", fields=("who", "access"), required=("who", "access"),
       verb="Change who may use"),
    Op("board_card_text", "act", "card", fields=("title", "text"), any_of=True, verb="Change the words of"),
    Op("feature_on", "act", "feature", verb="Turn on"),
    # Evals (F1255): a workflow's rubric, its golden set's cases, a run of the set, and stopping it, each a card.
    Op("eval_rubric", "act", "workflow", fields=("name", "criteria", "floor", "rubric"), any_of=True,
       verb="Score"),
    Op("eval_cases", "act", "workflow", fields=("cases", "count", "steer", "golden_set"), any_of=True,
       verb="Add test cases to"),
    Op("eval_run", "act", "workflow", fields=("golden_set",), verb="Run the eval of"),
    Op("eval_cancel", "act", "workflow", fields=("golden_set",), verb="Stop the eval of"),
    # Recordings (F1270): a kept fix used or dropped, a new name, or the recording gone, each a card.
    Op("recording_approve_edit", "act", "recording", fields=("edit_id",), required=("edit_id",),
       verb="Use the kept fix in"),
    Op("recording_discard_edit", "act", "recording", fields=("edit_id",), required=("edit_id",),
       verb="Drop the kept fix in"),
    Op("recording_rename", "act", "recording", fields=("name",), required=("name",), verb="Rename"),
    Op("recording_delete", "act", "recording", verb="Delete"),
    # A website widget, made or changed through the Widgets screen's own code (F1262).
    Op("widget_create", "act", "widget", fields=_WIDGET_FIELDS, required=("agent",), verb="Make"),
    Op("widget_update", "act", "widget", fields=(*_WIDGET_FIELDS, "on"), any_of=True, verb="Change"),
    Op("widget_delete", "act", "widget", verb="Delete"),
    Op("widget_rotate_key", "act", "widget", verb="Give a new key to"),
    Op("widget_logo", "act", "widget", fields=("path",), required=("path",), verb="Set the logo of"),
    Op("widget_logo_remove", "act", "widget", verb="Take the logo off"),
    # A chat channel that is connected, through the Channels screen's own code (F1263); connecting one is the Connect
    # card's, where its token is typed.
    Op("channel_connect", "act", "channel", fields=("platform", "agent"), required=("platform",), verb="Connect"),
    Op("channel_on", "act", "channel", verb="Turn on"),
    Op("channel_off", "act", "channel", verb="Turn off"),
    Op("channel_delete", "act", "channel", verb="Remove"),
    Op("channel_test", "act", "channel", verb="Send a test message on"),
    Op("channel_agents", "act", "channel", fields=("agents", "agent"), required=("agents",),
       verb="Change who answers on"),
    Op("channel_run_notifications", "act", "channel", verb="Send run notices to"),
    Op("channel_run_notifications_off", "act", "channel", verb="Stop sending run notices to"),
)

_SCHEDULE_TIME = ("schedule_type", "cron_expression", "interval_value", "interval_unit", "timezone")
#: What a schedule does with a time it missed (the machine was off, the fleet was down), as the Schedules screen sets
#: it (F1267): run it once as soon as it can, or skip it.
MISSED = ("run_immediately", "skip")

_WRITES = (
    Op("rename_workflow", "write", "workflow", "set_orchestration_meta", ("name",), ("name",), verb="Rename"),
    Op("update_workflow", "write", "workflow", "set_orchestration_meta",
       ("description", "max_total_turns", "timeout_minutes", "max_run_credits", "max_run_usd"), any_of=True,
       verb="Change the settings of"),
    Op("delete_workflow", "delete", "workflow", "delete_orchestration", verb="Delete"),
    Op("set_schedule", "write", "schedule", "update_schedule",
       (*_SCHEDULE_TIME, "prompt", "description", "missed_run_policy"), any_of=True, verb="Change"),
    # What it starts, another workflow or agent (F1267): the same schedule, its id, history and time kept.
    Op("retarget_schedule", "write", "schedule", "update_schedule", ("runs",), ("runs",),
       verb="Change what runs on"),
    Op("rename_schedule", "write", "schedule", "update_schedule", ("name",), ("name",), verb="Rename"),
    Op("delete_schedule", "delete", "schedule", "delete_schedule", verb="Delete"),
    Op("create_schedule", "create", "schedule", "create_schedule",
       ("runs", "prompt", *_SCHEDULE_TIME, "description", "missed_run_policy"), ("runs", "prompt"), verb="Make"),
    # A model's id, or what the agent needs, or the person's own words for one (F966): code fills the id.
    Op("set_agent_model", "write", "agent", "update_agent", ("model", "needs", "wish"), any_of=True,
       verb="Change the model of"),
    Op("set_agent_tools", "write", "agent", "update_agent", ("add", "remove"), any_of=True,
       verb="Change the tools of"),
    Op("set_agent_skills", "write", "agent", "attach_skills", ("add", "remove"), any_of=True,
       verb="Change the skills of"),
    Op("set_agent_instructions", "write", "agent", "update_agent", ("system_prompt",), ("system_prompt",),
       verb="Change the instructions of"),
    Op("rename_agent", "write", "agent", "update_agent", ("name",), ("name",), verb="Rename"),
    Op("set_tool_policy", "write", "agent", "update_agent", (*POLICY_CATEGORIES, "tools"), any_of=True,
       verb="Change the tool policy of"),
    Op("delete_agent", "delete", "agent", "delete_agent", verb="Delete"),
    Op("update_skill", "write", "skill", "update_skill", ("description", "body"), any_of=True, verb="Change"),
    # A library skill, through the Skills screen's own delete (F1266); a disk skill is the folder's.
    Op("delete_skill", "delete", "skill", "delete_skill", verb="Delete"),
    # The rest of an agent's settings, one op per section of the agent editor (F1259, `agent_census`; Naveen 10-31).
    Op("set_agent_settings", "write", "agent", "update_agent", AGENT_SETTINGS, any_of=True,
       verb="Change the settings of"),
    Op("set_agent_team", "write", "agent", "update_agent", TEAM_SETTINGS, any_of=True, verb="Change the team of"),
    Op("set_agent_browser", "write", "agent", "update_agent", BROWSER_SETTINGS, any_of=True,
       verb="Change the browser of"),
    Op("set_agent_code", "write", "agent", "update_agent", ("repos", "db_configs"), any_of=True,
       verb="Change the code access of"),
    # An agent's memory items, through the Memory screen's own doors (F1259): one corrected, one deleted.
    Op("edit_agent_memory", "write", "memory", "edit_agent_memory_item", ("content",), ("content",), verb="Change"),
    Op("delete_agent_memory", "delete", "memory", "delete_agent_memory_item", verb="Delete"),
    # One passage of a note or a text file, as it stands, and its new text; or a short note's whole text (F1258).
    Op("knowledge_edit_document", "write", "document", "edit_knowledge_document", ("find", "replace", "text"),
       any_of=True, verb="Change"),
    # A base's plain settings, as its screen sets them (F1276): its language, visitors' search, its monthly budget.
    Op("knowledge_settings", "write", "knowledge_base", "update_knowledge_base",
       ("language", "visitor_access", "budget_monthly_usd", "clear_budget"), any_of=True, verb="Change the settings of"),
    Op("delete_tool", "delete", "tool", "delete_custom_tool", verb="Delete"),
    # A new HTTP tool from its own facts (F1322; Naveen's pick, 11-04): `create` of a tool with its url is this op,
    # made by `create_http_tool` itself and held to that tool's own checks before the review.
    Op("create_tool", "create", "tool", "create_http_tool", HTTP_TOOL_FIELDS, ("description", "url"), verb="Make"),
    # An HTTP tool's address, method or what it does, changed in place (F1312b): never its headers or its key.
    Op("update_tool", "write", "tool", "update_http_tool", ("url", "method", "description", "from"), any_of=True,
       verb="Change"),
    # A Python tool's code, by passages quoted as they stand or its whole code, tested before the yes (F1325).
    Op("update_python_tool", "write", "tool", "update_python_tool",
       ("edits", "code", "description", "test_args", "needs_network"), any_of=True, verb="Change"),
    # The workspace's settings on Synapse AI's list (F1264; Naveen 10-30), each field a setting by its own name:
    # anything off the list is refused by name, with where it is done (`settings_allow`).
    Op("settings_set", "write", "settings", "set_settings", tuple(settings_allow.ALLOWED), any_of=True,
       verb="Change"),
)

#: What `create` hands to today's create path.
CREATES = ("agent", "skill", "tool", "openapi", "python_tool")
#: What `create` hands to the build route instead (F1242): a new workflow is a build, never refused here (prod 10-29:
#: refused, the model sent it again as an agent, and one agent was made of a two-agent workflow).
BUILDS = ("workflow",)

_HANDOFFS = (
    Op("edit_steps", "handoff", "workflow", fields=("what",), required=("what",), verb="Change the steps of"),
    Op("create", "handoff", "new", fields=("what",), required=("what",), verb="Make"),
    Op("use_recording", "handoff", "recording", fields=("what",), required=("what",), verb="Make a workflow that runs"),
)

#: An undo's re-create of what a change deleted, from its before-image (F797): code builds these from the card it
#: kept, never a model, so they are no second way to make a new agent, workflow or tool. Each comes back as a new
#: copy, which the review says.
_RESTORES = (
    # An undo of an agent's settings, team, browser or code access (F1259): its fields as they were, from the card.
    Op("set_agent_fields", "write", "agent", "update_agent",
       ("description", "memory", "knowledge", "boards", "planning", "skip_default_tools", "tools", "max_turns",
        "turn_limit_mode", "reasoning_effort", "max_output_tokens", "delegate_agent_ids", "team", "browser", "repos",
        "db_configs"), any_of=True, verb="Put back", internal=True),
    # An undo of a memory item's delete (F1259): written back through the Memory screen's own door, under its name.
    Op("restore_agent_memory", "create", "memory", "edit_agent_memory_item", ("item", "content"), ("item", "content"),
       verb="Bring back", internal=True),
    Op("restore_agent", "create", "agent", "create_agent",
       ("name", "description", "type", "tools", "system_prompt", "model", "delegate_agent_ids", "tool_policy"),
       ("name", "description", "type", "tools", "system_prompt"), verb="Bring back", internal=True),
    Op("restore_workflow", "create", "workflow", "create_orchestration",
       ("name", "description", "steps_json", "entry_step_id", "state_schema_json", "max_total_turns",
        "timeout_minutes", "max_run_credits", "max_run_usd"), ("name", "steps_json", "entry_step_id"),
       verb="Bring back", internal=True),
    Op("restore_tool", "create", "tool", "create_http_tool",
       ("name", "description", "url", "method", "input_schema_json", "body_template", "group"),
       ("name", "description", "url"), verb="Bring back", internal=True),
    Op("restore_python_tool", "create", "tool", "create_python_tool",
       ("name", "description", "code", "input_schema_json", "group"), ("name", "description", "code"),
       verb="Bring back", internal=True),
    # An undo of a skill's delete (F1266): written back to the library as it was, under its name.
    Op("restore_skill", "create", "skill", "create_skill", ("name", "description", "body"),
       ("name", "description", "body"), verb="Bring back", internal=True),
)

OPS: dict[str, Op] = {op.name: op for op in (*_ACTS, *_WRITES, *_HANDOFFS, *_RESTORES)}
#: Fields an op takes only from code's own undo (`change_undo`), never from the planner: checked as what an undo
#: builds (`issues(internal=True)`), refused by name from a model (F1312b: the inputs a change added, taken out again).
UNDO_ONLY: dict[str, tuple[str, ...]] = {"update_tool": ("drop_inputs",)}

#: What a model may propose: every op but the internal ones.
PLANNER_OPS = tuple(n for n, op in OPS.items() if not op.internal)
#: (Every button act in `builder_actions.ACTIONS` is an op of kind act, and only those: a test holds it, since this
#: module is imported while the builder's tools load and cannot read that table then.)

#: The registry tool each write calls, once each (a test holds every one served and its arguments its schema's).
WRITE_TOOLS = tuple(dict.fromkeys(op.tool for op in OPS.values() if op.tool))

#: The argument that names an act's target, as `prepare_action` takes it.
#: Acts whose target is the thing they make, named, never looked up among what exists (F1200).
MAKES_TARGET = frozenset({"board_create", "knowledge_create", "widget_create", "channel_connect", "support_request"})

_ACT_ARG = {"workflow": "workflow", "schedule": "schedule", "run": "run_id", "approval": "approval_id",
            "knowledge_base": "knowledge_base", "document": "doc_id", "board": "board", "card": "card_id",
            "feature": "feature", "recording": "recording", "widget": "widget", "channel": "channel",
            "agent": "agent", "dead_job": "job_id"}


# ── checking what a model wrote ───────────────────────────────────────────────────────────────────────────────────

def _is_list_of_text(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _agent_field_issue(op: Op, key: str, value: Any) -> str | None | bool:
    """An agent setting's own rule (F1259), by the editor's own values; False when `key` is not one of them."""
    from core.models import MAX_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS

    def whole(low: int, high: int) -> str | None:
        ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        return None if ok else f"`{key}` is a whole number from {low} to {high}"

    if key == "mode" and op.name == "set_agent_team":
        return None if value in ("one_at_a_time", "team") else "`mode` is one_at_a_time or team"
    if key == "mode" and op.name == "set_agent_browser":
        return None if value in ("read_only", "co_drive") else "`mode` is read_only or co_drive"
    if key in ("memory", "boards_own", "planning", "skip_default_tools", "helpers", "clear_spend_limit"):
        return None if isinstance(value, bool) else f"`{key}` is true or false"
    if key in ("knowledge", "sites", "sessions", "recordings", "repos", "db_configs"):
        return None if _is_list_of_text(value) else f"`{key}` is a list of names"
    if key == "max_turns":
        return whole(1, 200)  # the agent editor's own bounds, as each below
    if key in ("at_once", "task_budget"):
        return whole(1, 50)
    if key == "action_budget":
        return whole(1, 200)
    if key == "max_output_tokens":
        return whole(MIN_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
    if key == "turn_limit_mode":
        return None if value in ("ask", "continue", "stop") else "`turn_limit_mode` is ask, continue or stop"
    if key == "reasoning_effort":
        return None if value in ("off", "low", "medium", "high") else "`reasoning_effort` is off, low, medium or high"
    if key == "spend_limit_usd":
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else \
            "`spend_limit_usd` is a number of dollars above 0"
    return False


def _http_tool_field_issue(key: str, value: Any) -> str | None | bool:
    """A new HTTP tool's field's own rule (F1322); False when `key` is not one of them."""
    if key == "method":
        return None if isinstance(value, str) and value.strip().upper() in HTTP_METHODS else \
            f"`method` is one of {', '.join(HTTP_METHODS)}"
    if key == "headers":
        ok = isinstance(value, dict) and all(isinstance(k, str) and k.strip() and isinstance(v, str)
                                             for k, v in value.items())
        return None if ok else "`headers` maps a header's name to its text"
    if key == "input_schema":
        ok = isinstance(value, dict) and value.get("type", "object") == "object" and \
            isinstance(value.get("properties", {}), dict)
        return None if ok else "`input_schema` is a JSON Schema object, {type: object, properties: {…}}"
    if key == "body":
        return None if isinstance(value, (str, dict, list)) else "`body` is the request's text, or its JSON"
    if key == "from":
        curl = value.get("curl") if isinstance(value, dict) else None
        ok = isinstance(value, dict) and (
            (set(value) == {"curl"} and isinstance(curl, int) and not isinstance(curl, bool) and curl >= 1)
            or (set(value) == {"spec", "operation"} and all(isinstance(value[k], str) and value[k].strip()
                                                           for k in ("spec", "operation"))))
        return None if ok else ("`from` is {curl: n}, a curl the person pasted (the context's `curls`), or {spec, "
                                "operation}, an operation of a spec you read with read_api_spec")
    if key == "key":
        header, scheme = (value.get("header"), value.get("scheme", "")) if isinstance(value, dict) else (None, None)
        ok = (isinstance(value, dict) and set(value) <= {"header", "scheme"} and isinstance(header, str)
              and re.fullmatch(r"[A-Za-z0-9-]{1,64}", header.strip()) is not None
              and scheme in ("", None, *KEY_SCHEMES))
        # Never its value back, whatever was sent: these words go to the planner.
        return None if ok else ("`key` says where its key goes, {header: the header that carries it, scheme: Bearer, "
                                "Token or none}, never the key itself: the person gives that on a card after the yes")
    return False


def _code_field_issue(key: str, value: Any) -> str | None | bool:
    """A Python tool's change's field's own rule (F1325); False when `key` is not one of them."""
    if key == "edits":
        ok = isinstance(value, list) and 1 <= len(value) <= 20 and all(
            isinstance(e, dict) and set(e) <= {"find", "replace"} and isinstance(e.get("find"), str)
            and e["find"].strip() and isinstance(e.get("replace", ""), str) for e in value)
        return None if ok else ("`edits` is a list of {find: a passage of its code quoted exactly as it stands, "
                                "replace: its new text}")
    if key == "test_args":
        ok = isinstance(value, list) and 1 <= len(value) <= 3 and all(isinstance(a, dict) for a in value)
        return None if ok else "`test_args` is one to three sample inputs, each an object like a real call's"
    if key == "needs_network":
        return None if isinstance(value, bool) else "`needs_network` is true or false"
    return False


def _field_issue(op: Op, key: str, value: Any) -> str | None:
    if op.name == "update_python_tool" and (said := _code_field_issue(key, value)) is not False:
        return said
    if op.name in ("create_tool", "update_tool") and (said := _http_tool_field_issue(key, value)) is not False:
        return said
    if op.target == "agent" and op.name.startswith("set_agent_") and \
            (said := _agent_field_issue(op, key, value)) is not False:
        return said
    if key in ("add", "remove", "urls", "sites", "visitor_tools", "agents"):
        return None if _is_list_of_text(value) else f"`{key}` is a list of names"
    if key in _WIDGET_CHOICES and op.target == "widget":
        return None if value in _WIDGET_CHOICES[key] else f"`{key}` is one of {', '.join(_WIDGET_CHOICES[key])}"
    if key == "daily_cap":
        return None if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else \
            "`daily_cap` is a whole number of messages a day"
    if key == "on":
        return None if isinstance(value, bool) else "`on` is true or false"
    if key in POLICY_CATEGORIES:
        return None if value in DECISIONS else f"`{key}` is one of {', '.join(DECISIONS)}"
    if key == "tools" and op.name == "set_tool_policy":
        ok = isinstance(value, dict) and all(isinstance(k, str) and k and v in DECISIONS for k, v in value.items())
        return None if ok else f"`tools` maps a tool's name to one of {', '.join(DECISIONS)}"
    if key == "runs":
        ok = (isinstance(value, dict) and value.get("kind") in ("workflow", "agent")
              and any(isinstance(value.get(k), str) and value[k].strip() for k in ("id", "name")))
        return None if ok else "`runs` is {kind: workflow | agent, id or name} of what the schedule starts"
    if key == "missed_run_policy":
        return None if value in MISSED else f"`missed_run_policy` is {' or '.join(MISSED)}"
    if key == "schedule_type":
        return None if value in ("cron", "interval") else "`schedule_type` is cron or interval"
    if key == "timezone":
        from core.models_schedule import _is_timezone

        return None if isinstance(value, str) and _is_timezone(value) else \
            "`timezone` is a place's timezone, like Europe/Amsterdam (leave it out for the person's own)"
    if key == "interval_unit":
        return None if value in ("minutes", "hours", "days") else "`interval_unit` is minutes, hours or days"
    if key in ("interval_value", "max_total_turns", "timeout_minutes"):
        return None if isinstance(value, int) and not isinstance(value, bool) and value > 0 else \
            f"`{key}` is a whole number above 0"
    if key in ("max_run_credits", "max_run_usd"):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else \
            f"`{key}` is a number above 0"
    if key == "needs":
        from core.native_builder.models_menu import NEEDS

        return None if value in NEEDS else f"`needs` is one of {', '.join(NEEDS)}"
    if key == "status":
        return None if value in ("open", "done", "failed") else "`status` is open, done or failed"
    if key == "access" and op.name == "board_access":
        return None if value in ("add", "work", "none") else "`access` is add, work or none"
    if key == "criteria":
        ok = isinstance(value, list) and value and all(
            isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip() for c in value)
        return None if ok else "`criteria` is a list of {name, weight, guidance}"
    if key == "cases":
        ok = isinstance(value, list) and value and all(
            isinstance(c, dict) and isinstance(c.get("input"), str) and c["input"].strip() for c in value)
        return None if ok else "`cases` is a list of {name, input, notes}, each with its input"
    if key == "count":
        return None if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10 else \
            "`count` is a whole number from 1 to 10"
    if key == "floor":
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else "`floor` is a number"
    if key == "kind":
        return None if value in ("page", "sitemap", "site") else "`kind` is page, sitemap or site"
    if key == "replace":  # empty takes the passage out
        return None if isinstance(value, str) else "`replace` is text"
    if key == "language":
        from core.knowledge.kb_settings import TS_LANGUAGES

        return None if value in TS_LANGUAGES else f"`language` is one of {', '.join(TS_LANGUAGES)}"
    if key in ("visitor_access", "clear_budget"):
        return None if isinstance(value, bool) else f"`{key}` is true or false"
    if key == "budget_monthly_usd":
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else \
            "`budget_monthly_usd` is a number of dollars, 0 or more"
    return None if isinstance(value, str) and value.strip() else f"`{key}` is text"


def issues(op_name: str, target: dict[str, Any], fields: dict[str, Any], *, internal: bool = False) -> list[str]:
    """What is wrong with one proposed op, one line each, for the model to fix (empty when nothing is). `internal`:
    code built it (an undo's re-create may be one), never a model."""
    op = OPS.get(op_name)
    if op is None or (op.internal and not internal):
        return [f"`{op_name}` is not an op. The ops are: {', '.join(PLANNER_OPS)}."]
    found: list[str] = []
    kind = (target or {}).get("kind")
    if op.name == "create":
        if kind not in CREATES + BUILDS:
            found.append(f"`create` makes a new {', '.join(CREATES + BUILDS)}: target.kind is one of those")
    elif kind != op.target:
        found.append(f"`{op.name}` is about a {op.target}: target.kind is {op.target}")
    if not any(isinstance((target or {}).get(k), str) and target[k].strip() for k in ("id", "name")):
        found.append(f"`{op.name}` needs its target's id (or its name)")
    unknown = [k for k in fields if k not in op.fields and not (internal and k in UNDO_ONLY.get(op.name, ()))]
    if op.name == "settings_set":
        return found + _settings_issues(fields)
    if unknown:
        takes = f"it takes {', '.join(op.fields)}" if op.fields else "it takes no fields"
        found.append(f"`{op.name}` cannot set {', '.join(unknown)}: {takes}")
    # A re-create may need an empty list (an agent with no tools); never a value the card did not keep.
    left_out = [k for k in op.required if fields.get(k) is None or (not op.internal and fields.get(k) in ("", [], {}))]
    if left_out:
        found.append(f"`{op.name}` needs {', '.join(left_out)}")
    if op.any_of and not any(fields.get(k) not in (None, "", [], {}) for k in op.fields):
        found.append(f"`{op.name}` needs at least one of {', '.join(op.fields)}")
    for key in op.fields if not op.internal else ():  # a re-create's values are a stored record's; its tool checks them
        if key in fields and fields[key] not in (None, "", [], {}):
            if (why := _field_issue(op, key, fields[key])) is not None:
                found.append(f"`{op.name}`: {why}")
    if op.name == "knowledge_settings" and not found:
        if fields.get("clear_budget") is not None and fields.get("budget_monthly_usd") is not None:
            found.append("`knowledge_settings` sets a budget or clears it, not both")
        if fields.get("visitor_access") is True and len([k for k, v in fields.items() if v is not None]) > 1:
            # Opening a base to visitors is its own change, never bundled with another (8a, 10-31).
            found.append("`knowledge_settings`: opening a base to visitors is a change of its own; send the other "
                         "settings as another op")
    if op.name == "set_agent_browser" and not found and fields.get("mode") == "co_drive" and \
            len([k for k, v in fields.items() if v is not None]) > 1:
        # Letting a browser agent type and send is a change of its own, never bundled (Naveen 10-31).
        found.append("`set_agent_browser`: letting it type and send (co_drive) is a change of its own; send the other "
                     "settings as another op")
    if op.name == "update_python_tool" and not found:
        if bool(fields.get("edits")) == bool(fields.get("code")):
            found.append("`update_python_tool` takes its `edits` (passages quoted as they stand) or a short tool's "
                         "whole `code`: one of them")
        if not fields.get("test_args") and not internal:  # an undo puts back code that ran before: no samples
            found.append("`update_python_tool` needs `test_args`: one to three sample inputs its fix is tested on "
                         "before the person sees it")
    if op.name == "set_agent_team" and not found and fields.get("clear_spend_limit") and \
            fields.get("spend_limit_usd") is not None:
        found.append("`set_agent_team` sets a spend limit or clears it, not both")
    if op.name == "knowledge_edit_document" and not found:
        whole, passage = fields.get("text") not in (None, ""), fields.get("find") not in (None, "")
        if whole == passage or (passage and not isinstance(fields.get("replace"), str)):
            found.append("`knowledge_edit_document` takes the passage as it stands (`find`) and its new text "
                         "(`replace`, empty to take it out), or a short note's whole new text (`text`): one of those")
    if not found and not op.internal:
        found += [f"`{op.name}`: {why}" for why in _saved_as(op, fields)]
    return found


def as_planned(ops: list[Any]) -> list[Any]:
    """The ops as code reads them (F1322): `create` of an HTTP tool with its url, or with where code reads it from
    (`from`, F1312a), is `create_tool`, and each new tool comes before the ops that give it to an agent, the rest in
    the planner's order."""
    out: list[Any] = []
    for raw in ops:
        if isinstance(raw, dict) and raw.get("op") == "create" and (raw.get("target") or {}).get("kind") == "tool" \
                and isinstance(raw.get("fields"), dict) and ({"url", "from"} & set(raw["fields"])):
            raw = {**raw, "op": "create_tool"}
        out.append(raw)
    return sorted(out, key=lambda o: not (isinstance(o, dict) and o.get("op") == "create_tool"))


def _http_body(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def http_tool_reads(fields: dict[str, Any]) -> list[str]:
    """The arguments a new HTTP tool's URL, headers and body read (`{{ order_id }}`), as a call fills them."""
    from core import http_tool, templating

    texts = [str(fields.get("url") or ""), *(str(v) for v in (fields.get("headers") or {}).values()),
             _http_body(fields["body"]) if fields.get("body") not in (None, "") else ""]
    found: list[str] = []
    for text in texts:
        for reference in templating.references(text):
            if (name := http_tool._argument_of(reference)) and name not in found:
                found.append(name)
    return found


def http_tool_schema(fields: dict[str, Any]) -> dict[str, Any]:
    """Its inputs: the schema given, or, with none, each argument it reads, as text it needs."""
    if isinstance(fields.get("input_schema"), dict):
        return {"type": "object", **fields["input_schema"]}
    reads = http_tool_reads(fields)
    return {"type": "object", "properties": {n: {"type": "string"} for n in reads}, "required": reads}


def http_tool_origin(url: str) -> str:
    from urllib.parse import urlsplit

    got = urlsplit(url.strip())
    return f"{got.scheme}://{got.netloc}"


def http_tool_problems(fields: dict[str, Any]) -> list[str]:
    """What would make a new HTTP tool fail or send its key astray, by code (F1322): an argument its URL, headers or
    body read that its schema leaves out; for a tool that sends a key, a host it fills from its arguments."""
    from urllib.parse import urlsplit

    out: list[str] = []
    if isinstance(fields.get("input_schema"), dict):
        declared = set((fields["input_schema"].get("properties") or {}))
        if left := [n for n in http_tool_reads(fields) if n not in declared]:
            out.append(f"it reads {', '.join(left)}, which its input_schema leaves out")
    if isinstance(fields.get("key"), dict) and "{" in urlsplit(str(fields.get("url") or "").strip()).netloc:
        out.append("a tool that sends a key keeps its host fixed: its host takes no {{ }}")
    return out


def http_tool_args(name: str, fields: dict[str, Any]) -> dict[str, Any]:
    """`create_http_tool`'s arguments for a new HTTP tool (F1322): its fields under the tool's own names, its inputs
    as `http_tool_schema` says them; a tool that sends a key keeps to its host (`base_lock`, F944: what its Connect
    card shows the key goes to), set by code from the URL the person approves, never from a field."""
    url = str(fields.get("url") or "").strip()
    args: dict[str, Any] = {"name": name, "description": str(fields.get("description") or "").strip(), "url": url,
                            "method": str(fields.get("method") or "GET").strip().upper(),
                            "input_schema_json": json.dumps(http_tool_schema(fields))}
    if fields.get("headers"):
        args["headers_json"] = json.dumps(fields["headers"], ensure_ascii=False)
    if fields.get("body") not in (None, ""):
        args["body_template"] = _http_body(fields["body"])
    if _text_of(fields.get("group")):
        args["group"] = _text_of(fields.get("group"))
    if isinstance(fields.get("key"), dict):
        args["base_lock"] = http_tool_origin(url)
    return args


def _text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _settings_issues(fields: dict[str, Any]) -> list[str]:
    """A settings change's: each name on the list here (the host's narrowing too), each value by its own check. An
    empty value is a value here (a model left to the default), so every one is checked, never skipped."""
    if not fields:
        return ["`settings_set` needs at least one setting, by its name, with its new value"]
    here = settings_allow.allowed()
    return [f"`settings_set`: {settings_allow.refusal(k)}" if k not in here else
            f"`settings_set`: {why}" for k, v in fields.items()
            if k not in here or (why := settings_allow.value_issue(k, v)) is not None]


def _saved_as(op: Op, fields: dict[str, Any]) -> list[str]:
    """What the save itself would refuse, by the product's own rules, said before the review rather than after the
    yes (the branch review's finding 10): a schedule's time, a run cap."""
    from core.models_schedule import ScheduleInvalid, validate_timing
    from core.orchestration.run_cap import clean

    out: list[str] = []
    if op.name in ("set_schedule", "create_schedule"):
        time = _schedule_time(fields)
        whole = "cron_expression" in time or ("interval_value" in time and "interval_unit" in time)
        if op.name == "create_schedule" and not whole:
            out.append("a new schedule needs its time: cron_expression, or interval_value and interval_unit")
        elif whole:
            try:
                validate_timing(time)
            except ScheduleInvalid as why:
                out.append(str(why))
    for key in ("max_run_credits", "max_run_usd"):
        if op.name == "update_workflow" and key in fields:
            try:
                clean(fields[key], credits=key == "max_run_credits")
            except ValueError as why:
                out.append(str(why))
    if op.name == "create_tool":
        out += http_tool_problems(fields)
    return out


# ── what an op calls, and what it leaves ──────────────────────────────────────────────────────────────────────────

def act_arguments(op: Op, target_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """`prepare_action`'s arguments for an act on a resolved target."""
    name_arg = "name" if op.name in MAKES_TARGET else _ACT_ARG[op.target]
    return {"action": op.name, name_arg: target_id, **{k: v for k, v in fields.items() if k in op.fields}}


def _merged(names: list[str], add: list[str] | None, remove: list[str] | None) -> list[str]:
    out = [n for n in names if n not in (remove or [])]
    return out + [n for n in (add or []) if n not in out]


def _policy(live: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    policy = dict(live.get("tool_policy") or {})
    rules = {k: fields[k] for k in POLICY_CATEGORIES if k in fields}
    rules.update({f"tool:{name}": d for name, d in (fields.get("tools") or {}).items()})
    for key, decision in rules.items():
        if decision == "inherit":
            policy.pop(key, None)
        else:
            policy[key] = decision
    return policy


def _schedule_time(fields: dict[str, Any]) -> dict[str, Any]:
    out = {k: fields[k] for k in _SCHEDULE_TIME if k in fields}
    if "cron_expression" in out:
        out["cron_expression"] = str(out["cron_expression"]).strip()
        out.setdefault("schedule_type", "cron")
    elif "interval_value" in out or "interval_unit" in out:
        out.setdefault("schedule_type", "interval")
    return out


def _starts(runs: dict[str, Any]) -> dict[str, Any]:
    """What a schedule's `runs` is stored as."""
    return {"target_type": "orchestration" if runs["kind"] == "workflow" else "agent", "target_id": runs["id"]}


def expected(op: Op, fields: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the target must read afterwards, field by field; None for a delete (it must be gone)."""
    live = live or {}
    name = op.name
    if op.kind == "delete":
        return None
    if name in ("rename_workflow", "rename_schedule", "rename_agent"):
        return {"name": str(fields["name"]).strip()}
    if name in ("update_workflow", "update_skill"):
        return {k: fields[k] for k in op.fields if k in fields}
    if name == "knowledge_settings":
        out = {k: fields[k] for k in ("language", "visitor_access", "budget_monthly_usd") if fields.get(k) is not None}
        if fields.get("clear_budget") is True:
            out["budget_monthly_usd"] = None
        return out
    if name in AGENT_SECTIONS or name in ("set_agent_fields", "edit_agent_memory", "restore_agent_memory"):
        return _agent_expected(name, fields, live)
    if name == "knowledge_edit_document":
        if fields.get("text") not in (None, ""):
            return {"text": fields["text"]}
        return {"text": str(live.get("text") or "").replace(fields["find"], fields.get("replace") or "", 1)}
    if name == "set_schedule":
        return {**_schedule_time(fields),
                **{k: fields[k] for k in ("prompt", "description", "missed_run_policy") if k in fields}}
    if name == "retarget_schedule":
        return _starts(fields["runs"])
    if name == "update_python_tool":
        from core.python_edits import apply_edits

        new = str(fields["code"]) if fields.get("code") is not None else \
            apply_edits(str(live.get("code") or ""), fields.get("edits") or [])[0]
        return {"code": new, **({"description": str(fields["description"]).strip()}
                                if fields.get("description") is not None else {})}
    if name == "update_tool":
        out = {}
        if fields.get("url"):
            out["url"] = str(fields["url"]).strip()
        if fields.get("method"):
            out["method"] = str(fields["method"]).strip().upper()
        if fields.get("description") is not None:
            out["description"] = str(fields["description"]).strip()
        return out
    if name == "create_tool":
        args = http_tool_args("", fields)
        return {"tool_type": "http", "url": args["url"], "method": args["method"], "description": args["description"],
                **({"body": args["body_template"]} if "body_template" in args else {})}
    if name == "create_schedule":
        return {**_starts(fields["runs"]), "prompt": fields["prompt"], **_schedule_time(fields),
                **{k: fields[k] for k in ("description", "missed_run_policy") if fields.get(k)}}
    if name == "set_agent_model":
        return {"model": str(fields["model"]).strip()}
    if name == "set_agent_tools":
        return {"tools": _merged(list(live.get("tools") or []), fields.get("add"), fields.get("remove"))}
    if name == "set_agent_skills":
        return {"skills": _merged(list(live.get("skills") or []), fields.get("add"), fields.get("remove"))}
    if name == "set_agent_instructions":
        return {"system_prompt": fields["system_prompt"]}
    if name == "set_tool_policy":
        return {"tool_policy": _policy(live, fields)}
    if name == "settings_set":
        return dict(fields)
    if op.internal:
        return {"name": fields["name"]}
    raise ValueError(f"{name} leaves nothing to check")


def _agent_expected(name: str, fields: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """What an agent (or one of its memory items) reads after an F1259 op: each as the agent document holds it. Names
    are ids by now: `check_change` resolves them first."""
    from core.models import AgentBrowser, AgentTeam

    given = {k: v for k, v in fields.items() if v is not None}
    if name == "set_agent_fields":
        return dict(fields)  # its before-image, a field that was unset among them
    if name in ("edit_agent_memory", "restore_agent_memory"):
        return {"content": str(fields.get("content") or "").strip()}
    if name == "set_agent_code":
        return given
    if name == "set_agent_settings":
        out = {k: v for k, v in given.items() if k not in ("boards_own", "helpers")}
        if "boards_own" in given:
            out["boards"] = {**(live.get("boards") or {}), "own": given["boards_own"]}
        if "helpers" in given:
            out["tools"] = _merged(list(live.get("tools") or []), ["spawn_subtask"] if given["helpers"] else None,
                                   None if given["helpers"] else ["spawn_subtask"])
        return out
    if name == "set_agent_team":
        out: dict[str, Any] = {}
        if "add" in given or "remove" in given:
            out["delegate_agent_ids"] = _merged(list(live.get("delegate_agent_ids") or []), given.get("add"),
                                                given.get("remove"))
        way = {k: given[k] for k in ("mode", "at_once", "task_budget", "spend_limit_usd") if k in given}
        if given.get("clear_spend_limit"):
            way["spend_limit_usd"] = None
        if way:
            out["team"] = {**AgentTeam().model_dump(), **(live.get("team") or {}), **way}
        return out
    return {"browser": {**AgentBrowser().model_dump(), **(live.get("browser") or {}), **given}}


def call(op: Op, target_id: str, fields: dict[str, Any], live: dict[str, Any] | None,
         *, name: str = "") -> tuple[str, dict[str, Any]]:
    """The one registry call a write makes: its tool and exact arguments (what an approved review covers)."""
    assert op.tool is not None, f"{op.name} calls no tool"
    after = expected(op, fields, live)
    if op.kind == "delete":
        key = {"workflow": "orch_id", "agent": "agent_id", "schedule": "schedule_id", "tool": "name",
               "memory": "item", "skill": "name"}[op.target]
        return op.tool, {key: target_id}
    if op.name == "set_agent_fields":
        return op.tool, {"agent_id": target_id, "fields": after}
    if op.internal:
        return op.tool, {k: v for k, v in fields.items() if k in op.fields and v is not None}
    if op.target == "memory":
        return op.tool, {"item": target_id, "content": after["content"]}
    if op.target == "workflow":
        return op.tool, {"orch_id": target_id, **after}
    if op.name == "create_schedule":
        return op.tool, {"name": name, **after}
    if op.name == "create_tool":
        return op.tool, http_tool_args(name, fields)
    if op.name == "update_python_tool":
        from core.python_edits import code_sha

        # The code it was read from: one changed since is refused, not written over (F1258's way, `was_sha256`).
        return op.tool, {"name": target_id, **after, "was_sha256": code_sha(str((live or {}).get("code") or ""))}
    if op.name == "update_tool":
        # An undo's `drop_inputs` (change_undo, never the planner's): the inputs this change added, taken out again.
        return op.tool, {"name": target_id, **after,
                         **({"drop_inputs": list(fields["drop_inputs"])} if fields.get("drop_inputs") else {})}
    if op.target == "schedule":
        return op.tool, {"schedule_id": target_id, **after}
    if op.name == "set_agent_skills":
        return op.tool, {"agent_id": target_id, "names": after["skills"]}
    if op.target == "agent":
        return op.tool, {"agent_id": target_id, "fields": after}
    if op.target == "skill":
        return op.tool, {"name": target_id, **after}
    if op.target == "settings":
        return op.tool, {"changes": after}
    if op.target == "knowledge_base":
        args = {k: v for k, v in after.items() if v is not None}
        if "budget_monthly_usd" in after and after["budget_monthly_usd"] is None:
            args["clear_budget"] = True
        return op.tool, {"knowledge_base": target_id, **args}
    if op.target == "document":
        from core.product_tools.knowledge import text_sha256

        # Written only over the text the review was made from (F1258, TRI1): a document changed since is said.
        return op.tool, {"doc_id": target_id, "text": after["text"],
                         "was_sha256": text_sha256(str((live or {}).get("text") or ""))}
    raise ValueError(f"{op.name} has no call")


def _same(want: Any, got: Any) -> bool:
    if isinstance(want, str) and isinstance(got, str):
        return want.strip() == got.strip()
    if isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool):
        return float(want) == float(got)
    return want == got


def differs(record: dict[str, Any] | None, want: dict[str, Any] | None) -> dict[str, tuple[Any, Any]]:
    """Each field of `want` the record does not read as: {field: (wanted, read)}. A delete's `want` is None: the
    record must be gone."""
    if want is None:
        return {} if record is None else {"exists": (False, True)}
    if record is None:
        return {"exists": (True, False)}
    return {k: (v, record.get(k)) for k, v in want.items() if not _same(v, record.get(k))}
