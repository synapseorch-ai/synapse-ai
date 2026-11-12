"""Each of a Human step's fields is its own answer — F1557 (HUM1; Naveen 11-12: "the multiple input boxes that I set is
not working properly in the human step").

A Human step's answer is a mapping keyed by each field's name (F75), and the form keeps one value per name: the app's
form, the editor's setup card and chat's card all read and write `values[field.name]`. Nothing made sure a field had a
name of its own, and three ways in left several boxes one answer:

- the shipped template packs (Developer's two review steps and its clarify step, Starter's and Simple's) named their
  fields `key`, which nothing reads: every field there had no name, so the PR review's Decision and its Feedback were one
  box — the answer was `{"undefined": "<the feedback>"}`, the decision lost;
- the editor's Add field named a new field after the count (`field_<n + 1>`), so after a delete it repeated a name: the
  comment typed into one box was lost to the Yes / No in the other;
- a name cleared in the editor left a field with none.

And nothing refused one: neither the editor's checks nor the server's validator looked at a field's name.

Now a field named `key` is read as named so (a stored spelling is read, never rewritten: the engine, the validator and
the editor alike, and the packs now write `name`); Add field takes the first name no field has; and a field with no name,
or with a name another field of the step has, is refused — an issue on the server, an error in the editor, in the same
words, the repair first.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest
from _fakes import seed as S

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "backend" / "examples"
FIELDS_TS = ROOT / "frontend/src/components/orchestration/humanFields.ts"
#: The Developer pack's PR review, as it shipped.
PACKED = [{"key": "pr_decision", "label": "Decision", "type": "select", "options": ["approve", "request_changes"]},
          {"key": "pr_feedback", "label": "Feedback or change requests (optional)", "type": "textarea"}]


def _human_steps(document) -> list[dict]:
    found = []
    if isinstance(document, dict):
        if document.get("type") == "human" and "human_fields" in document:
            found.append(document)
        for value in document.values():
            found += _human_steps(value)
    elif isinstance(document, list):
        for value in document:
            found += _human_steps(value)
    return found


def _orch(fields: list[dict], *after: dict, orch_id: str = "orch_f1557") -> dict:
    steps = [{"id": "review", "name": "Review", "type": "human", "human_prompt": "Review it", "output_key": "review",
              "human_fields": fields, "next_step_id": after[0]["id"] if after else "end"},
             *after, {"id": "end", "name": "End", "type": "end"}]
    return {"id": orch_id, "name": "F1557", "entry_step_id": "review", "steps": steps}


def _names_said(orch: dict) -> list[str]:
    return [i for i in _validate_orchestration(orch)["issues"] if "Human step 'review'" in i and "name" in i]


class TestThePacksNameEveryField:
    @pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name)
    def test_each_field_has_a_name_of_its_own(self, path):
        for step in _human_steps(json.loads(path.read_text())):
            names = [f.get("name") for f in step["human_fields"]]
            assert all(isinstance(n, str) and n.strip() for n in names), (step.get("id"), step["human_fields"])
            assert len(set(names)) == len(names), (step.get("id"), names)
            assert not any("key" in f for f in step["human_fields"]), (step.get("id"), step["human_fields"])


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class TestAFieldNamedKeyIsReadAsNamed:
    """A workflow imported from a pack before this keeps its `key`s: the run asks and keeps each field by it."""

    async def test_the_run_asks_and_keeps_each_field(self):
        from core.models_orchestration import Orchestration
        from core.orchestration import answers
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import SENTINEL
        from core.orchestration.state import SharedState

        orch = S.make_orchestration(id="orch_f1557_packed", entry_step_id="review", steps=[
            {"id": "review", "name": "Review", "type": "human", "human_prompt": "Review the PR",
             "human_fields": PACKED, "output_key": "review", "next_step_id": "say"},
            {"id": "say", "name": "Say", "type": "print", "output_key": "said", "next_step_id": None,
             "print_content": "{{ state.review.pr_decision }} / {{ state.review.pr_feedback }}"},
        ])
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [e async for e in engine.run("go", run_id="run_orch_f1557_packed")]
        asked = events[-1]
        assert [f.get("name") for f in asked["fields"]] == ["pr_decision", "pr_feedback"], asked["fields"]
        run = (await SharedState.restore("run_orch_f1557_packed")).run
        assert [f.get("name") for f in run.human_fields] == ["pr_decision", "pr_feedback"]

        outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=_server()),
                                                  "run_orch_f1557_packed",
                                                  {"pr_decision": "APPROVE", "pr_feedback": "fix the tests"})
        resumed = []
        while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
            resumed.append(event)
        said = [e["response"] for e in resumed if e.get("type") == "final" and e.get("orch_step_id") == "say"]
        assert said == ["approve / fix the tests"], resumed  # the dropdown checked as one, by its name

    def test_the_validator_reads_its_fields(self):
        gate = {"id": "gate", "name": "Gate", "type": "if_else", "if_condition": "state.review.pr_decision == 'approve'",
                "if_true_step_id": "end", "if_false_step_id": "end"}
        verdict = _validate_orchestration(_orch(PACKED, gate))
        assert verdict["ok"], verdict["issues"]

    def test_a_name_is_kept_beside_a_key(self):
        from core.orchestration.human_fields import named

        assert named([{"name": "a", "key": "b"}, {"key": "c"}, {"key": " "}, "x"]) == \
            [{"name": "a", "key": "b"}, {"key": "c", "name": "c"}, {"key": " "}, "x"]


class TestANamelessOrRepeatedFieldIsRefused:
    def test_two_fields_of_one_name(self):
        [said] = _names_said(_orch([{"name": "field_2", "type": "string", "label": "Comment"},
                                    {"name": "field_2", "type": "boolean", "label": "Urgent?"}]))
        assert said.startswith("Human step 'review' has two fields named 'field_2': give each a name of its own"), said
        assert "one value per name" in said, said

    @pytest.mark.parametrize("field", [{"type": "string", "label": "Comment"}, {"name": "  ", "label": "Comment"},
                                       {"name": "", "key": "", "label": "Comment"}])
    def test_a_field_with_no_name(self, field):
        [said] = _names_said(_orch([{"name": "ok", "type": "boolean"}, field]))
        assert said.startswith("Human step 'review' field 2 ('Comment') has no name: give it one"), said
        assert "state.review.<name>" in said, said

    def test_distinct_names_pass(self):
        assert _names_said(_orch([{"name": "comment"}, {"name": "urgent", "type": "boolean"},
                                  {"name": "tier", "type": "choice", "options": ["Gold", "Silver"]}])) == []


# ── The editor ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the editor's form fields are TypeScript")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                          f"import * as F from {json.dumps(str(FIELDS_TS))};\nconsole.log(JSON.stringify({script}));"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


SHAPES = {
    "repeated": [{"name": "field_2", "type": "string", "label": "Comment"},
                 {"name": "field_2", "type": "boolean", "label": "Urgent?"}],
    "nameless": [{"name": "ok", "type": "boolean"}, {"type": "string", "label": "Comment"}],
    "blank": [{"name": " ", "label": "Comment"}],
    "three_of_one": [{"name": "a"}, {"name": "a"}, {"name": "a"}],
    "packed": PACKED,
    "fine": [{"name": "comment"}, {"name": "urgent", "type": "boolean"}],
}


def _server_verdicts(fields: list[dict]) -> set[tuple]:
    import re

    out = set()
    for said in _names_said(_orch(fields)):
        if m := re.match(r"Human step 'review' field (\d+)(?: \('.*?'\))? has no name", said):
            out.add(("nameless", int(m[1])))
        elif m := re.match(r"Human step 'review' has \w+ fields named '(.*?)'", said):
            out.add(("repeated", m[1]))
    return out


class TestTheEditor:
    def test_add_field_never_repeats_a_name(self):
        assert _node("F.nextHumanFieldName([{name: 'field_2'}])") == "field_3"
        assert _node("F.nextHumanFieldName([])") == "field_1"
        assert _node("F.nextHumanFieldName([{name: 'field_1'}, {name: 'field_3'}])") == "field_4"
        assert _node("F.nextHumanFieldName([{name: 'comment'}, {name: 'urgent'}])") == "field_3", "the count, as before"
        assert _node("F.nextHumanFieldName([{name: 'comment'}, {key: 'field_2'}])") == "field_3"

    def test_a_field_named_key_is_read_as_named(self):
        assert _node(f"F.namedHumanFields({json.dumps(PACKED)}).map((f) => f.name)") == ["pr_decision", "pr_feedback"]

    def test_the_packs_review_keeps_both_answers(self):
        answer = _node(f"(() => {{ const f = F.namedHumanFields({json.dumps(PACKED)}); "
                       f"const v = {{...F.initialHumanValues(f), pr_decision: 'approve', pr_feedback: 'fix it'}}; "
                       f"return F.humanAnswer(f, v); }})()")
        assert answer == {"pr_decision": "approve", "pr_feedback": "fix it"}

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_the_editor_says_what_the_server_says(self, shape):
        fields = SHAPES[shape]
        editor = _node(f"F.humanFieldNameIssues({json.dumps(fields)}, 'review')")
        assert {(v["kind"], v.get("index") or v.get("name")) for v in editor} == _server_verdicts(fields), shape
        said = {v["kind"]: v["message"] for v in editor}
        if "nameless" in said:
            assert "has no name: give it one" in said["nameless"] and "state.review.<name>" in said["nameless"], said
        if "repeated" in said:
            assert "give each a name of its own" in said["repeated"] and "one value per name" in said["repeated"], said

    def test_each_is_wired(self):
        src = ROOT / "frontend/src/components/orchestration"
        steps = (src / "panel/steps.tsx").read_text()
        # A Human step's Add field and a model step's Add answer field (F1553's twin) alike.
        assert steps.count("name: nextHumanFieldName(fields)") == 2 and "`field_${fields.length + 1}`" not in steps
        reader = (src / "readOrchestration.ts").read_text()
        assert "namedHumanFields(" in reader
        validate = (src / "validate.ts").read_text()
        start = validate.index("humanFieldNameIssues(")
        assert "error(" in validate[start:start + 300], validate[start:start + 300]
