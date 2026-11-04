"""
A change is ops from one closed table: what each may set, the one registry tool it calls, and what the target must
read afterwards (F792, SAI2-P2, D308).

The old change route had a model write a markdown plan and another model (the operator, 33 tools) carry it out, so
what a change could do was whatever the operator's tools allowed, and F755's card-or-review gate read tool names out
of the markdown. Now the change planner proposes `{op, target, fields}` and code decides the rest from
`change_ops.OPS`: a field an op does not take is refused by name (F784's rule), each write is exactly one call of a
registry tool (audited by id, F697) whose schema takes those arguments, and the same `expected` is the no-op check
before and the effect check after. Making a new agent, skill, HTTP tool or OpenAPI import is not an op: it is handed
to today's create path, which holds their checks (Naveen's split, 10-22).
"""
import pytest

from core.builder_actions import ACTIONS
from core.native_builder import change_ops as C
from core.native_builder.objects import CHANGE_OPS, WRITE_OPS, Change


def test_every_button_act_is_an_op_and_its_kind_says_card():
    acts = {n for n, op in C.OPS.items() if op.is_act}
    assert acts == set(ACTIONS)
    assert not any(C.OPS[n].is_write for n in ACTIONS)


def test_the_model_s_enum_is_the_table():
    assert set(CHANGE_OPS) == set(C.PLANNER_OPS) == {n for n, op in C.OPS.items() if not op.internal}
    assert set(WRITE_OPS) == {n for n in C.PLANNER_OPS if not C.OPS[n].is_act}


def test_an_undo_s_re_create_is_code_s_only():
    # F1198 (BLD1): a deleted Python tool's re-create, its own. F1259: an agent's settings set back as they were, and a
    # deleted memory item written back. F1266: a deleted skill written back to the library.
    assert {n for n, op in C.OPS.items() if op.internal} == {"restore_agent", "restore_workflow", "restore_tool",
                                                              "restore_python_tool", "set_agent_fields",
                                                              "restore_agent_memory", "restore_skill"}
    assert C.issues("restore_agent", {"kind": "agent", "name": "x"}, {}) == [
        f"`restore_agent` is not an op. The ops are: {', '.join(C.PLANNER_OPS)}."]


@pytest.mark.parametrize("tool", ["create_agent", "create_agents", "create_skill", "create_http_tool",
                                  "import_openapi", "create_python_tool"])
def test_making_something_new_is_handed_over_not_an_op(tool):
    """No op a model may propose reaches a create tool: only an undo's re-create, which code builds from what a
    change deleted (F797), may call one."""
    assert tool not in C.OPS
    assert tool not in {C.OPS[n].tool for n in C.PLANNER_OPS}
    assert {op.name for op in C.OPS.values() if op.tool == tool} <= {"restore_agent", "restore_tool",
                                                                      "restore_python_tool", "restore_skill"}
    assert C.OPS["create"].kind == "handoff"


def test_the_operator_s_writes_on_what_exists_are_ops():
    for tool in ("create_schedule", "update_skill", "delete_custom_tool", "update_schedule", "update_agent",
                 "attach_skills", "set_orchestration_meta", "delete_agent", "delete_orchestration",
                 "delete_schedule"):
        assert tool in C.WRITE_TOOLS


def test_every_write_tool_is_a_product_tool_that_acts():
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    for tool in C.WRITE_TOOLS:
        entry = registry.get(tool)
        assert entry is not None, tool
        assert entry.kind == "act" and entry.scope == "build", tool


# ── what a model wrote ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_field_an_op_does_not_take_is_refused_by_name():
    found = C.issues("rename_agent", {"kind": "agent", "id": "a1"}, {"name": "X", "rubric_id": "r"})
    assert found == ["`rename_agent` cannot set rubric_id: it takes name"]


@pytest.mark.parametrize("op, target, fields, words", [
    ("rename_agent", {"kind": "agent", "id": "a1"}, {}, "needs name"),
    ("set_schedule", {"kind": "schedule", "id": "s1"}, {}, "needs at least one of"),
    ("rename_agent", {"kind": "workflow", "id": "w1"}, {"name": "x"}, "is about a agent"),
    ("rename_agent", {"kind": "agent"}, {"name": "x"}, "needs its target's id"),
    ("set_tool_policy", {"kind": "agent", "id": "a1"}, {"open_world": "ask"}, "one of allow, confirm"),
    ("set_agent_tools", {"kind": "agent", "id": "a1"}, {"add": "web_search"}, "a list of names"),
    ("update_workflow", {"kind": "workflow", "id": "w1"}, {"max_run_credits": -1}, "a number above 0"),
    ("create_schedule", {"kind": "schedule", "name": "Mondays"}, {"runs": {"kind": "board", "id": "b"},
                                                                    "prompt": "go"}, "`runs` is"),
    # A new workflow is the build's, handed to it (F1242), so a kind nothing makes is the refused one.
    ("create", {"kind": "board", "name": "x"}, {"what": "a board"}, "makes a new agent"),
    ("fly", {"kind": "agent", "id": "a1"}, {}, "is not an op"),
])
def test_what_is_wrong_is_said_for_a_retry(op, target, fields, words):
    found = C.issues(op, target, fields)
    assert found and any(words in line for line in found), found


def test_a_well_formed_op_has_no_issues():
    assert C.issues("set_tool_policy", {"kind": "agent", "name": "Support bot"},
                    {"open_world": "confirm", "tools": {"send_email": "deny"}}) == []
    assert C.issues("create", {"kind": "tool", "name": "Mailchimp"}, {"what": "an HTTP tool for Mailchimp"}) == []
    assert C.issues("create", {"kind": "workflow", "name": "Weekly post"}, {"what": "a workflow"}) == []


# ── what a write calls ──────────────────────────────────────────────────────────────────────────────────────────

AGENT = {"id": "a1", "name": "Front desk", "tools": ["read_file", "web_search"], "skills": ["triage"],
         "tool_policy": {"destructive": "confirm"}, "model": "gemini-flash-latest"}


@pytest.mark.parametrize("op, fields, tool, args", [
    ("rename_agent", {"name": " Desk "}, "update_agent", {"agent_id": "a1", "fields": {"name": "Desk"}}),
    ("set_agent_model", {"model": "claude-haiku-4-5"}, "update_agent",
     {"agent_id": "a1", "fields": {"model": "claude-haiku-4-5"}}),
    ("set_agent_tools", {"add": ["send_email"], "remove": ["read_file"]}, "update_agent",
     {"agent_id": "a1", "fields": {"tools": ["web_search", "send_email"]}}),
    ("set_agent_skills", {"remove": ["triage"], "add": ["billing"]}, "attach_skills",
     {"agent_id": "a1", "names": ["billing"]}),
    ("set_tool_policy", {"open_world": "confirm", "destructive": "inherit", "tools": {"send_email": "deny"}},
     "update_agent", {"agent_id": "a1", "fields": {"tool_policy": {"open_world": "confirm",
                                                                   "tool:send_email": "deny"}}}),
    ("delete_agent", {}, "delete_agent", {"agent_id": "a1"}),
])
def test_an_agent_op_is_one_call_built_from_what_the_agent_has(op, fields, tool, args):
    assert C.call(C.OPS[op], "a1", fields, AGENT) == (tool, args)


def test_a_schedule_s_new_time_says_its_type():
    assert C.call(C.OPS["set_schedule"], "s1", {"cron_expression": " 0 3 * * * "}, {}) == (
        "update_schedule", {"schedule_id": "s1", "cron_expression": "0 3 * * *", "schedule_type": "cron"})
    tool, args = C.call(C.OPS["create_schedule"], "", {
        "runs": {"kind": "workflow", "id": "w1"}, "prompt": "Send the digest", "cron_expression": "0 9 * * 1"},
        None, name="Mondays")
    assert (tool, args) == ("create_schedule", {
        "name": "Mondays", "target_type": "orchestration", "target_id": "w1", "prompt": "Send the digest",
        "cron_expression": "0 9 * * 1", "schedule_type": "cron"})


def test_a_workflow_op_and_a_skill_op():
    assert C.call(C.OPS["update_workflow"], "w1", {"max_run_credits": 3}, {}) == (
        "set_orchestration_meta", {"orch_id": "w1", "max_run_credits": 3})
    assert C.call(C.OPS["delete_tool"], "Mailchimp", {}, {}) == ("delete_custom_tool", {"name": "Mailchimp"})
    assert C.call(C.OPS["update_skill"], "triage", {"body": "New"}, {}) == (
        "update_skill", {"name": "triage", "body": "New"})


def _samples():
    return [
        ("rename_workflow", {"name": "N"}), ("update_workflow", {"max_run_credits": 3, "description": "d"}),
        ("delete_workflow", {}), ("set_schedule", {"interval_value": 2, "interval_unit": "hours"}),
        ("rename_schedule", {"name": "N"}), ("delete_schedule", {}),
        ("create_schedule", {"runs": {"kind": "agent", "id": "a1"}, "prompt": "p", "cron_expression": "0 9 * * *"}),
        ("set_schedule", {"missed_run_policy": "run_immediately"}),
        ("retarget_schedule", {"runs": {"kind": "workflow", "id": "w1"}}),
        ("settings_set", {"model": "model-x", "prompt_cache_enabled": False}),
        ("set_agent_model", {"model": "m"}), ("set_agent_tools", {"add": ["x"]}),
        ("set_agent_skills", {"add": ["x"]}), ("set_agent_instructions", {"system_prompt": "s"}),
        ("rename_agent", {"name": "N"}), ("set_tool_policy", {"open_world": "deny"}), ("delete_agent", {}),
        ("update_skill", {"description": "d"}), ("delete_tool", {}),
        ("knowledge_edit_document", {"text": "Open 8 to 6."}), ("knowledge_settings", {"language": "english"}),
        ("set_agent_settings", {"max_turns": 12}), ("set_agent_team", {"at_once": 4}),
        ("set_agent_browser", {"action_budget": 60}), ("set_agent_code", {"repos": ["repo_shop"]}),
        ("set_agent_fields", {"max_turns": 3}), ("edit_agent_memory", {"content": "Refunds in 60 days."}),
        ("delete_agent_memory", {}),
        ("restore_agent_memory", {"item": "a1/refund-policy", "content": "Refunds in 30 days."}),
        ("restore_agent", {"name": "N", "description": "d", "type": "conversational", "tools": [],
                           "system_prompt": "s", "model": "m"}),
        ("restore_workflow", {"name": "N", "steps_json": "[]", "entry_step_id": "s1", "max_run_credits": 2}),
        ("restore_tool", {"name": "N", "description": "d", "url": "https://x.example/api", "method": "GET"}),
        ("restore_python_tool", {"name": "N", "description": "d", "code": "print(1)\n"}),
        ("delete_skill", {}),
        ("restore_skill", {"name": "N", "description": "d", "body": "Use the house style."}),
    ]


def test_every_write_is_sampled():
    assert {n for n, _ in _samples()} == {n for n, op in C.OPS.items() if op.is_write}


@pytest.mark.parametrize("op, fields", _samples())
def test_every_write_s_arguments_are_its_tool_s_own(op, fields):
    """The class rule: a built call names only arguments its registry tool declares, and every one it needs."""
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    tool, args = C.call(C.OPS[op], "t1", fields, AGENT, name="N")
    schema = registry.get(tool).input_schema
    props = schema.get("properties") or {}
    assert set(args) <= set(props), (tool, set(args) - set(props))
    assert set(schema.get("required") or []) <= set(args) | {"confirm"}, tool


# ── what the target must read ───────────────────────────────────────────────────────────────────────────────────

def test_a_no_op_and_an_effect_are_the_same_comparison():
    want = C.expected(C.OPS["set_schedule"], {"cron_expression": "0 3 * * *"}, {})
    assert C.differs({"cron_expression": "0 3 * * *", "schedule_type": "cron", "name": "x"}, want) == {}
    assert C.differs({"cron_expression": "0 2 * * *", "schedule_type": "cron"}, want) == {
        "cron_expression": ("0 3 * * *", "0 2 * * *")}


def test_a_delete_must_leave_nothing_and_a_write_needs_its_target():
    assert C.differs(None, None) == {}
    assert C.differs({"id": "a1"}, None) == {"exists": (False, True)}
    assert C.differs(None, {"name": "x"}) == {"exists": (True, False)}


def test_numbers_compare_as_numbers():
    assert C.differs({"max_run_credits": 3.0}, {"max_run_credits": 3}) == {}


def test_a_change_object_takes_the_new_kinds():
    change = Change.model_validate({"ops": [{"op": "update_skill", "target": {"kind": "skill", "name": "triage"},
                                             "fields": {"body": "x"}},
                                            {"op": "create", "target": {"kind": "openapi", "name": "Billing"},
                                             "fields": {"what": "import their spec"}}]})
    assert [op.is_act for op in change.ops] == [False, False]
    assert change.has_writes


@pytest.mark.parametrize("op, fields, words", [
    ("create_schedule", {"runs": {"kind": "workflow", "id": "w1"}, "prompt": "Go"}, "needs its time"),
    ("create_schedule", {"runs": {"kind": "workflow", "id": "w1"}, "prompt": "Go", "cron_expression": "every day"},
     "not a valid cron expression"),
    ("set_schedule", {"cron_expression": "61 * * * *"}, "not a valid cron expression"),
    ("set_schedule", {"interval_value": 20000, "interval_unit": "minutes"}, "from 1 to 10,000"),
    ("update_workflow", {"max_run_credits": 0.5}, "1 or more"),
])
def test_what_the_save_would_refuse_is_said_before_the_review(op, fields, words):
    """The check uses the product's own rules (`models_schedule.validate_timing`, `run_cap.clean`), so nothing the
    person approves is refused after the yes (the branch review's finding 10)."""
    kind = "workflow" if op == "update_workflow" else "schedule"
    found = C.issues(op, {"kind": kind, "name": "x"}, fields)
    assert any(words in line for line in found), found


def test_a_time_the_save_takes_has_no_issue():
    assert C.issues("set_schedule", {"kind": "schedule", "id": "s"}, {"cron_expression": "0 3 * * *"}) == []
    assert C.issues("set_schedule", {"kind": "schedule", "id": "s"}, {"interval_unit": "hours"}) == []  # a part
    assert C.issues("update_workflow", {"kind": "workflow", "id": "w"}, {"max_run_credits": 3}) == []


def _shown(value):
    import json as _json

    return value if isinstance(value, str) else _json.dumps(value, ensure_ascii=False)


#: A value for every field an op declares (MCP2's re-check, B: a hand-made sample that sets no `description` let
#: a new schedule's line leave it out though its call sent it). Each is distinctive, so its presence is checked.
_EVERY = {"language": "english", "visitor_access": True, "budget_monthly_usd": 25, "clear_budget": False,
          "text": "OPEN 8 TO 6", "find": "OPEN 9 TO 5", "replace": "OPEN 8 TO 6", "name": "New name", "description": "PAYS THE VENDOR", "prompt": "SEND THE DIGEST", "model": "model-x",
          "cron_expression": "0 3 * * *", "schedule_type": "cron", "timezone": "Europe/Amsterdam", "interval_value": 6, "interval_unit": "hours",
          "runs": {"kind": "workflow", "id": "w1", "name": "Nightly digest"}, "add": ["send_email"],
          "remove": ["read_file"], "system_prompt": "BE BRIEF", "tools": {"send_email": "deny"},
          "destructive": "confirm", "open_world": "deny", "undeclared": "allow", "max_total_turns": 7,
          "timeout_minutes": 9, "max_run_credits": 3, "max_run_usd": 2.5, "body": "THE SKILL'S TEXT",
          "needs": "reasoning", "wish": "A MODEL WISH",
          # An agent's sections (F1259): `mode` is a team's or a browser's, one value serves both lines.
          "memory": True, "knowledge": ["kb_handbook"], "boards_own": True, "planning": False,
          "skip_default_tools": True, "helpers": True, "max_turns": 12, "turn_limit_mode": "stop",
          "reasoning_effort": "high", "max_output_tokens": 4096, "mode": "co_drive", "at_once": 4, "task_budget": 9,
          "spend_limit_usd": 1.5, "clear_spend_limit": False, "sites": ["portal.example.com"],
          "sessions": ["sess_portal"], "recordings": ["rec_login"], "action_budget": 60, "repos": ["repo_shop"],
          "db_configs": ["db_orders"], "content": "THE MEMORY'S TEXT", "missed_run_policy": "run_immediately"}


def _every_field(op_name):
    if op_name == "settings_set":  # each setting is a field of its own (F1264): a few of each kind stand for them
        return {"model": "model-x", "prompt_cache_enabled": False, "max_output_tokens": 4096,
                "tool_policy_defaults": {"destructive": "confirm"}}
    return {k: _EVERY[k] for k in C.OPS[op_name].fields}


@pytest.mark.parametrize("op", list(C.OPS))
def test_no_op_s_line_says_its_noun_twice(op):
    """MCP2's re-check (C): `Make the schedule the schedule "Mondays"`. Every sentence that uses an op's verb names its
    target's kind after it, so a verb never carries the noun itself."""
    from core.native_builder.change_tools import _words

    o = C.OPS[op]
    kind = "workflow" if o.target == "workflow" else o.target.replace("_", " ")
    assert f"the {kind}" not in o.verb, (op, o.verb)
    fields = {k: _EVERY[k] for k in o.fields if k in _EVERY}
    line = _words(o, {"name": "Mondays"}, fields, "")
    assert line.count(f"the {kind}") == 1, line


@pytest.mark.parametrize("op", [n for n, o in C.OPS.items() if o.is_write and not o.internal and o.kind != "delete"])
def test_every_write_s_review_line_shows_what_its_call_sets(op):
    fields = _every_field(op)
    """MCP2's review, finding 2: the person approves what is done. A new schedule's line said only what it starts,
    though its call also sets the prompt and the time. Each write's line is now built from what its call leaves
    (`expected`), and every value its call sends, but the target's own id, is in its line."""
    from core.native_builder.change_tools import _words

    o = C.OPS[op]
    tool, args = C.call(o, "t1", fields, AGENT, name="N")
    line = _words(o, {"name": "N", "id": "t1"}, fields, "", C.expected(o, fields, AGENT))
    # Ids name the target; a document's `was_sha256` is the fingerprint of the text the review was read from, a guard
    # on the write (F1258), not a value it sets.
    skip = {"orch_id", "agent_id", "schedule_id", "target_id", "target_type", "doc_id", "was_sha256"}
    # A write's values ride in `fields` (update_agent) or `changes` (set_settings, F1264): each is checked.
    sent = {**{k: v for k, v in args.items() if k not in ("fields", "changes")}, **(args.get("fields") or {}),
            **(args.get("changes") or {})}
    for key, value in sent.items():
        if key in skip or value == "t1":
            continue
        assert _shown(value) in line, (op, key, value, line)
    if op == "create_schedule":
        assert '"Nightly digest"' in line or "Nightly digest" in line


def test_an_undo_s_re_create_says_it_brings_back_what_was_there():
    from core.native_builder.change_tools import _words

    line = _words(C.OPS["restore_agent"], {"name": "Support bot"}, {"name": "Support bot", "system_prompt": "s"}, "",
                  {"name": "Support bot"})
    assert line == 'Bring back the agent "Support bot" — as it was before it was deleted'


def test_the_planner_s_own_words_are_not_on_a_write_s_line():
    """MCP2's note 3: code's facts and the model's gloss are told apart; a write's line is code's alone."""
    from core.native_builder.change_tools import _words

    o = C.OPS["rename_agent"]
    assert _words(o, {"name": "Front desk"}, {"name": "Desk"}, "", {"name": "Desk"}) == \
        'Rename the agent "Front desk" — to "Desk"'
