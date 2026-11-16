"""A Tool step's set arguments never read a key only some arms of a branch write (F1617).

A Switch sent each message to one of three drafters, each writing its own key (`draft_billing`, `draft_technical`,
`draft_other`), and one Tool step after the join saved the draft with set arguments reading all three. A run takes one
arm, so the other keys were never written, the arguments could not be set ("… is not in the run's state"), and every
run failed. F1346 refuses a read that breaks without its key on a way a run can take with it unset, for a Transform's
code and a Loop's list; a Tool step's set arguments break the same way and were not among them. Now they are, and the
refusal says the repair: one key every arm writes, or a save in each arm.
"""
from __future__ import annotations

import pytest

from core.builder_tools import _validate_orchestration

ARMS = ("billing", "technical", "other")


def _drafter(arm: str, key: str, nxt: str = "save_draft") -> dict:
    return {"id": f"draft_{arm}", "name": f"Draft {arm} reply", "type": "llm", "model": "gpt-4o",
            "prompt_template": f"Draft a {arm} reply to: {{{{ state.user_input }}}}", "output_key": key,
            "next_step_id": nxt}


def _save(sid: str = "save_draft", content: str = "", nxt: str = "done") -> dict:
    return {"id": sid, "name": "Save the draft", "type": "tool", "forced_tool": "vault_write", "max_turns": 1,
            "tool_arguments": {"path": "replies/{{ state.classify.category }}.md", "content": content},
            "output_key": sid, "next_step_id": nxt}


def _desk(*, shared: bool = False, per_arm: bool = False, schema: dict | None = None) -> dict:
    """The drafting desk: classify → switch → a drafter per arm → one save after the join. `shared`: every drafter
    writes `draft`; `per_arm`: each arm saves its own draft and there is no shared save."""
    steps = [
        {"id": "classify", "name": "Classify", "type": "llm", "model": "gpt-4o",
         "prompt_template": "Classify {{ state.user_input }}", "output_key": "classify",
         "output_fields": [{"name": "category", "type": "choice", "options": list(ARMS)}], "next_step_id": "route"},
        {"id": "route", "name": "Route", "type": "switch", "switch_expression": "state.classify.category",
         "switch_cases": {"billing": "draft_billing", "technical": "draft_technical"},
         "switch_default_step_id": "draft_other"},
    ]
    if per_arm:
        for arm in ARMS:
            steps.append(_drafter(arm, f"draft_{arm}", nxt=f"save_{arm}"))
            steps.append(_save(f"save_{arm}", f"{{{{ state.draft_{arm} }}}}"))
    elif shared:
        steps += [_drafter(arm, "draft") for arm in ARMS]
        steps.append(_save(content="{{ state.draft }}"))
    else:
        steps += [_drafter(arm, f"draft_{arm}") for arm in ARMS]
        steps.append(_save(content="{{ state.draft_billing }}{{ state.draft_technical }}{{ state.draft_other }}"))
    steps.append({"id": "done", "name": "Done", "type": "end"})
    return {"id": "orch_desk", "name": "Drafting desk", "entry_step_id": "classify", "steps": steps,
            "state_schema": schema or {}}


def _about(issues: list[str], key: str) -> list[str]:
    return [i for i in issues if f"state.{key}" in i]


class TestAToolStepsArguments:
    def test_a_save_after_the_join_reading_each_arms_key_is_refused(self):
        verdict = _validate_orchestration(_desk())
        assert not verdict["ok"], verdict
        for arm in ARMS:
            said = _about(verdict["issues"], f"draft_{arm}")
            assert len(said) == 1, (arm, verdict["issues"])
            for words in ("save_draft", f"draft_{arm}", "route"):
                assert words in said[0], (words, said[0])

    def test_the_refusal_says_the_repair(self):
        said = _about(_validate_orchestration(_desk())["issues"], "draft_billing")[0]
        assert "every arm" in said or "each arm" in said, said

    @pytest.mark.parametrize("orch", [_desk(shared=True), _desk(per_arm=True)], ids=["one-key-every-arm",
                                                                                      "a-save-in-each-arm"])
    def test_a_key_every_arm_writes_or_a_save_in_each_arm_passes(self, orch):
        issues = _validate_orchestration(orch)["issues"]
        assert not [i for i in issues if "save_" in i and "state.draft" in i], issues

    def test_a_key_written_before_the_branch_passes(self):
        """`classify` is written before the Switch, so every arm reaches the save with it set."""
        assert _about(_validate_orchestration(_desk(shared=True))["issues"], "classify") == []

    def test_a_default_in_the_schema_passes(self):
        schema = {f"draft_{arm}": {"type": "str", "default": ""} for arm in ARMS}
        issues = _validate_orchestration(_desk(schema=schema))["issues"]
        assert not [i for i in issues if "state.draft_" in i], issues
