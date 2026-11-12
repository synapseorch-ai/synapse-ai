"""A condition that decides on a model's own words is warned, with its fix; and the editor says what the server says —
F1553's follow-ups (BLD4; Naveen's picks, 11-12).

F1553 has the builder declare a model step's answer fields, and the validator refuse a field the step does not
declare. A workflow built before it may still decide on the model's words: BLD4's triage built `state.classify.strip()
.upper() == 'URGENT'`, `state.analyze_sentiment.startswith('NEGATIVE')` and `"high" in state.review.lower()`, and a
2-star review that asked for a sentence first got the thank-you email; a safe clause went to legal. Naveen (pick 1):
warn, never refuse, and offer the fix. So:

- the server's check (`validate_orchestration`, the builder's and MCP's) has a **warning**, never an issue, on a
  condition (If/Else, Switch, a Loop's while and break) that reads an llm step's text (or a plain agent's) as words:
  a text method on it, a comparison with a quoted word, a quoted word `in` it, it `in` a list of words. "It reads the
  model's own words; declare an answer field" — a whole read for its length, a field it declares, a Tool's text, are
  left alone. Runs are unchanged;
- Build with AI, when a plan (a new one, or a change to that workflow) holds such a condition, has it back once as a
  structure finding — declare the field and rewrite the condition — and the card says it after (the offer);
- the editor (`validate.ts`, pick 3) says the same as the server as it is edited: F1549's field its writer never
  writes, F1553's field a model step does not declare, and this warning, from one module, `fieldChecks.ts`, held to
  the server's own verdicts on the same workflows (node runs it, its types stripped).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]
CHECKS_TS = ROOT / "frontend/src/components/orchestration/fieldChecks.ts"
WORDS = "reads the model's own words; declare an answer field"


def _orch(*steps: dict) -> dict:
    steps = [dict(s) for s in steps]
    for here, after in zip(steps, steps[1:]):
        if here.get("type") not in ("if_else", "switch", "end"):
            here.setdefault("next_step_id", after["id"])
    if steps[-1].get("type") != "end":
        steps[-1].setdefault("next_step_id", "end")
        steps.append({"id": "end", "name": "End", "type": "end"})
    return {"id": "orch_words", "name": "Words", "entry_step_id": steps[0]["id"], "steps": steps}


def _classify(kind: str = "llm", **over) -> dict:
    step = {"id": "classify", "name": "Classify", "type": kind, "prompt_template": "URGENT or NORMAL?",
            "output_key": "classify", **over}
    if kind == "agent":
        step.setdefault("agent_id", "agent_c")
    return step


def _gate(condition: str) -> dict:
    return {"id": "route", "name": "Route", "type": "if_else", "if_condition": condition,
            "if_true_step_id": "after", "if_false_step_id": "end"}


AFTER = {"id": "after", "name": "After", "type": "llm", "prompt_template": "Write it.", "output_key": "after"}
PLAIN = {"agent_c": {"id": "agent_c", "name": "C", "type": "agent", "tools": []}}

WORDY = [
    "state.classify.strip().upper() == 'URGENT'",          # BLD4's Sonnet r01
    "state.classify.startswith('NEGATIVE')",                # Haiku r06
    '"high" in state.classify.lower() and "risk" in state.classify.lower()',  # Sonnet's c1
    "state.classify == 'URGENT'",
    "'URGENT' != state.classify",
    "state.classify in ['URGENT', 'HIGH']",
    "str(state.classify).lower() == 'urgent'",
    "{{ state.classify.strip() == 'URGENT' }}",
]
NOT_WORDY = [
    "len(state.classify) > 500",
    "state.classify",
    "state.user_input.strip() == 'go'",
]


def _warned(orch: dict, agents=None) -> list[str]:
    verdict = _validate_orchestration(orch, agents=agents)
    assert not [i for i in verdict["issues"] if WORDS in i], "a warning, never an issue"
    return [w for w in verdict.get("warnings") or [] if WORDS in w]


class TestTheServerWarns:
    @pytest.mark.parametrize("condition", WORDY)
    def test_a_decision_on_an_llm_steps_words(self, condition):
        [said] = _warned(_orch(_classify(), _gate(condition), AFTER))
        assert "'route'" in said and "'classify'" in said and "output_fields" in said, said
        assert "runs as it is today" in said.lower(), said

    @pytest.mark.parametrize("condition", NOT_WORDY)
    def test_what_is_not_one(self, condition):
        assert _warned(_orch(_classify(), _gate(condition), AFTER)) == []

    def test_a_declared_field_is_not_words(self):
        fields = [{"name": "urgency", "type": "choice", "options": ["URGENT", "NORMAL"]}]
        assert _warned(_orch(_classify(output_fields=fields), _gate("state.classify.urgency == 'URGENT'"),
                             AFTER)) == []

    def test_a_tools_text_is_not_a_models(self):
        tool = {"id": "classify", "name": "Classify", "type": "tool", "forced_tool": "get_status",
                "output_key": "classify"}
        assert _warned(_orch(tool, _gate("state.classify.strip() == 'ok'"), AFTER)) == []

    def test_a_switch_and_a_loops_condition(self):
        switch = {"id": "route", "name": "Route", "type": "switch", "switch_expression": "state.classify.lower()",
                  "switch_cases": {"urgent": "after"}, "switch_default_step_id": "end"}
        assert _warned(_orch(_classify(), switch, AFTER))
        loop = {"id": "again", "name": "Again", "type": "loop", "loop_step_ids": ["classify"],
                "loop_while_condition": "state.classify != 'DONE'", "loop_max_iterations": 5}
        assert _warned(_orch(loop, _classify(), AFTER))

    def test_an_agent_step_when_its_agent_is_known(self):
        orch = _orch(_classify("agent"), _gate("state.classify.startswith('NEGATIVE')"), AFTER)
        assert _warned(orch, PLAIN) and _warned(orch) == []

    def test_the_run_is_unchanged(self):
        assert _validate_orchestration(_orch(_classify(), _gate(WORDY[0]), AFTER))["ok"] is True


# ── Build with AI offers the fix ───────────────────────────────────────────────────────────────────────────────────

PLAN_STEPS = [
    {"id": "classify", "type": "llm", "name": "Classify", "does": "Says urgent or normal",
     "prompt": "Reply URGENT or NORMAL.\n\n{{ state.user_input }}", "next": "route"},
    {"id": "route", "type": "if_else", "name": "Urgent?", "does": "Branches on it",
     "condition": "state.classify.strip().upper() == 'URGENT'", "branches": {"true": "escalate", "false": "reply"}},
    {"id": "escalate", "type": "llm", "name": "Escalate", "does": "Writes the note", "prompt": "Write it.",
     "next": "end"},
    {"id": "reply", "type": "llm", "name": "Reply", "does": "Writes the reply", "prompt": "Write it.", "next": "end"},
]


async def _examined(plan: dict, state: dict | None = None):
    from core.native_builder import build_plan

    return await build_plan.examine(plan, None, ws=build_plan.Workspace(), state=state or {"user_message": "x"})


class TestTheBuilderOffersTheFix:
    async def test_a_new_plan_has_it_back_as_a_structure_finding(self):
        found = await _examined({"kind": "create", "title": "Triage", "summary": "Triages.", "steps": PLAN_STEPS})
        [said] = [f for f in found.structure if WORDS in f]
        assert "classify" in said and "`config.output_fields`" in said and "route" in said, said

    async def test_a_change_to_a_workflow_that_has_one_offers_it(self):
        from core.native_builder import build_plan
        from core.native_builder.compile import compile_plan
        from core.native_builder import objects as O

        saved = compile_plan(O.Plan.from_draft({"kind": "create", "title": "Triage", "summary": "Triages.",
                                                "steps": PLAN_STEPS})).orchestration
        state = {"user_message": "make the reply friendlier", "intent": "update",
                 build_plan.EXISTING_KEY: {"orchestration": saved}}
        edit = {"kind": "update", "title": "Triage", "summary": "Triages.",
                "edits": [{"op": "update_step", "step_id": "reply", "fields": {"prompt": "Write a warm reply."}}]}
        found = await _examined(edit, state)
        assert any(WORDS in f for f in found.structure), (found.errors, found.structure)
        assert any(WORDS in n for n in found.notes), "the card says it after"

    async def test_declared_it_is_not_offered(self):
        steps = [dict(PLAN_STEPS[0], config={"output_fields": [{"name": "urgency", "type": "choice",
                                                                  "options": ["URGENT", "NORMAL"]}]}),
                 dict(PLAN_STEPS[1], condition="state.classify.urgency == 'URGENT'"), *PLAN_STEPS[2:]]
        found = await _examined({"kind": "create", "title": "Triage", "summary": "Triages.", "steps": steps})
        assert not [f for f in found.structure if WORDS in f]


# ── The editor says what the server says ───────────────────────────────────────────────────────────────────────────

def _node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the editor's checks are TypeScript")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return run.stdout


def _editor(orch: dict, agents=None) -> list[dict]:
    """`fieldChecks.ts`'s verdicts on `orch`: each {stepId, key, field?, kind}."""
    out = _node(f"import {{ fieldChecks }} from {json.dumps(str(CHECKS_TS))};\n"
                f"console.log(JSON.stringify(fieldChecks({json.dumps(orch['steps'])}, "
                f"{json.dumps(list((agents or {}).values())) if agents is not None else 'undefined'})));")
    return json.loads(out)


def _server(orch: dict, agents=None) -> set[tuple]:
    """The server's verdicts on `orch`, in `fieldChecks`' terms."""
    import re

    verdict = _validate_orchestration(orch, agents=agents)
    out = set()
    for i in verdict["issues"]:
        if m := re.match(r"Step '(\w+)' reads `(\w+)` from state\.(\w+)", i):
            out.add((m[1], m[3], m[2], "field"))
    for w in verdict.get("warnings") or []:
        if WORDS in w and (m := re.match(r"Step '(\w+)' decides on \w+ step '\w+''s answer as text .*?state\.(\w+)",
                                         w)):
            out.add((m[1], m[2], None, "words"))
    return out


YES_NO = {"name": "approved", "type": "boolean", "label": "Approve?"}
ASK = {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "OK?", "output_key": "ask"}
CASES = {
    "words": (_orch(_classify(), _gate(WORDY[0]), AFTER), None),
    "keyword": (_orch(_classify(), _gate(WORDY[2]), AFTER), None),
    "undeclared": (_orch(_classify(), _gate("state.classify.risky == True"), AFTER), None),
    "declared_other": (_orch(_classify(output_fields=[{"name": "risky", "type": "boolean"}]),
                             _gate("state.classify.is_risky"), AFTER), None),
    "declared": (_orch(_classify(output_fields=[{"name": "risky", "type": "boolean"}]),
                       _gate("state.classify.risky"), AFTER), None),
    "template": (_orch(_classify(), {**AFTER, "prompt_template": "Why: {{ state.classify.reason }}"}), None),
    "transform": (_orch(_classify(), {"id": "after", "name": "After", "type": "transform", "output_key": "after",
                                      "transform_code": "result = state['classify']['risky']"}), None),
    "fieldless_signoff": (_orch(ASK, _gate("state.ask.approved == True"), AFTER), None),
    "signoff": (_orch({**ASK, "human_fields": [YES_NO]}, _gate("state.ask.approved"), AFTER), None),
    "signoff_other": (_orch({**ASK, "human_fields": [YES_NO]}, _gate("state.ask.feedback"), AFTER), None),
    "evaluator": (_orch({"id": "judge", "name": "Judge", "type": "evaluator", "output_key": "judge",
                         "route_map": {"yes": "after", "no": "end"}}, AFTER,
                        {**AFTER, "id": "after2", "prompt_template": "{{ state.judge.reason }}"}), None),
    "agent_known": (_orch(_classify("agent"), _gate("state.classify.risky"), AFTER), PLAIN),
    "agent_unknown": (_orch(_classify("agent"), _gate("state.classify.risky"), AFTER), None),
    "text_method": (_orch(_classify(), _gate("len(state.classify) > 3"), AFTER), None),
}


class TestTheEditorSaysWhatTheServerSays:
    @pytest.mark.parametrize("case", sorted(CASES))
    def test_the_same_verdicts(self, case):
        orch, agents = CASES[case]
        editor = {(v["stepId"], v["key"], v.get("field"), v["kind"]) for v in _editor(orch, agents)}
        assert editor == _server(orch, agents), (case, editor, _server(orch, agents))

    def test_the_editor_shows_them_as_warnings(self):
        source = (ROOT / "frontend/src/components/orchestration/validate.ts").read_text()
        assert "from './fieldChecks'" in source and "fieldChecks(" in source
        start = source.index("fieldChecks(orch.steps")
        assert "warning(" in source[start:start + 400], source[start:start + 400]

    def test_each_verdict_has_its_sentence(self):
        said = {v["kind"]: v["message"] for v in _editor(*CASES["words"]) + _editor(*CASES["undeclared"])}
        assert "reads the model's own words; declare an answer field" in said["words"], said
        assert "answer fields" in said["field"], said
