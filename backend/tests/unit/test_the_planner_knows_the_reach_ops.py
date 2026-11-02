"""The one planner is told about Synapse AI's reach (REACH1's rules 15–19 and rule 10's fix; D340, TRI1's review).

The ops and topics REACH1 built (F1260, F1261, F1264, F1267, F1270, F1271) reached nothing until the planner's own
prompt named them: rule 10 even said a recording was renamed or deleted only in the app. These pin:

- both topic lists name every read_workspace topic the rules use (REACH2's widgets and channels too), and each topic
  they name is one that exists;
- every op the rules name is one of the change table's, so a rule never sends the planner to an op code refuses;
- rule 10 sends a recording's rename, delete and kept fixes to their ops, and keeps editing its steps the app's;
- the prompt renders unchanged with the workspace's variables: agent prompts are rendered with them
  (react_engine), so a reference written like a real one ({{ global.NAME }}) would be filled with a value, the very
  thing rule 19 says the planner never sees.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLANNER = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" / "planner.json"


@pytest.fixture(scope="module")
def prompt() -> str:
    return json.loads(PLANNER.read_text("utf-8"))["system_prompt"]


def _rule(prompt: str, n: int) -> str:
    found = re.search(rf"\n{n}\. (.*?)(?=\n\d+\. |\n\n)", prompt, re.S)
    assert found, f"rule {n} is not in # Changing"
    return found.group(1)


def test_both_topic_lists_name_the_reach_topics_and_only_real_ones(prompt):
    from core.builder_readers import TOPICS

    lists = re.findall(r"answers by topic \(([^)]*)\)", prompt)
    assert len(lists) == 2 and lists[0] == lists[1]
    named = [t.strip() for t in lists[0].split(",")]
    assert {"run", "variables", "settings", "savings"} <= set(named)
    # REACH2's rules 13 and 14 read these (TRI1, 11-02): every topic a rule reads is in the lists.
    assert {"widgets", "channels"} <= set(named)
    assert set(named) <= set(TOPICS), set(named) - set(TOPICS)


@pytest.mark.parametrize("n, ops", [
    (10, {"use_recording", "recording_rename", "recording_delete", "recording_approve_edit", "recording_discard_edit"}),
    (15, {"set_agent_model", "update_workflow", "edit_steps", "resume_run"}),
    (17, {"settings_set"}),
    (18, {"set_schedule", "retarget_schedule"}),
])
def test_each_op_a_rule_names_is_the_change_tables(prompt, n, ops):
    from core.native_builder import change_ops as C

    said = _rule(prompt, n)
    for op in ops:
        assert op in said, (n, op)
        assert op in C.PLANNER_OPS, (n, op)


def test_rule_10_keeps_only_editing_a_recordings_steps_in_the_app(prompt):
    said = _rule(prompt, 10)
    assert "Renaming, editing or deleting a recording is done on its page" not in said
    assert "Editing its steps is done on its page in the app." in said


def test_the_topics_the_rules_read_exist(prompt):
    from core.builder_readers import TOPICS

    for n, topic in ((15, "run"), (16, "savings"), (17, "settings"), (19, "variables")):
        assert f"topic `{topic}`" in _rule(prompt, n) and topic in TOPICS


def test_a_key_is_never_asked_for_in_the_chat(prompt):
    said = _rule(prompt, 17)
    assert "Settings › Vault or on its Connect card, never in this chat" in said


def test_the_prompt_renders_unchanged_with_the_workspaces_variables(prompt):
    from core import templating

    values = {"name": "LEAKED-1", "NAME": "LEAKED-2", "x": "LEAKED-3", "api_key": "LEAKED-4"}
    assert templating.render(prompt, {"global": values}) == prompt
