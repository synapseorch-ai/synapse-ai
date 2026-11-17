"""A read is judged by the writers that can reach it, on every way a run can take (F1621, part 2).

The answer-field rules judged a key by every step that writes it anywhere in the workflow. So a field one arm's
writer has and the other's lacks passed after the join, a Transform writing the key anywhere (even after the read)
switched the rules off, and a Loop over a field two arms declare differently was judged by whichever writer came
last in the list. Each read is now judged by the writers a run can reach it from (`state_reads.Walk.reaching`); a
read no writer reaches is judged by all of them, as before.

What each finds, and whether it refuses: a refusal only where the run certainly fails on that way.
- A field one reaching writer has and another lacks: a warning, naming the one that lacks it.
- A writer of unknown shape that cannot reach the read no longer switches the field rules off: the read is refused
  as those rules refuse it.
- A Loop over a field a reaching writer declares as no list: refused, naming that writer (it fails on that way).
- Two Tool steps on two arms writing one key: a field only one answers is a warning; a read only the other reaches
  is held to its tool's declaration, as one tool's is.
- An Ingest step reached with its records' key unset: refused, naming the way (the step fails there).
- An input_key read before any step writes it: a warning, as a prompt's read is.
"""
from __future__ import annotations

from core.builder_tools import _tool_answer_reads, _validate_orchestration
from core.orchestration import step_answers
from core.orchestration.output_fields import json_schema


def _orch(entry: str, *steps: dict, schema: dict | None = None) -> dict:
    return {"id": "orch_reach", "name": "Harbor & Pine reach", "entry_step_id": entry, "state_schema": schema or {},
            "steps": [*steps, {"id": "end", "name": "End", "type": "end"}]}


def _if(true: str, false: str, sid: str = "cond") -> dict:
    return {"id": sid, "name": sid, "type": "if_else", "if_condition": "'rush' in str(state.user_input)",
            "if_true_step_id": true, "if_false_step_id": false}


def _human(sid: str, nxt: str, *fields: str, key: str = "review") -> dict:
    return {"id": sid, "name": sid, "type": "human", "human_prompt": "Is the invoice right?", "output_key": key,
            "next_step_id": nxt, "human_fields": [{"name": f, "type": "boolean", "label": f"{f}?"} for f in fields]}


def _decide(nxt: str = "end") -> dict:
    return {"id": "decide", "name": "decide", "type": "if_else", "if_condition": "state.review.approved",
            "if_true_step_id": nxt, "if_false_step_id": nxt}


def _about(found: list[str], *words: str) -> list[str]:
    return [s for s in found if all(w in s for w in words)]


# ── a field one reaching writer has and another lacks ─────────────────────────────────────────────────────────────

class TestAFieldOneArmLacks:
    def test_is_a_warning_naming_the_writer_that_lacks_it(self):
        verdict = _validate_orchestration(_orch("cond", _if("ask_signoff", "ask_plain"),
                                                _human("ask_signoff", "decide", "approved"),
                                                _human("ask_plain", "decide"), _decide()))
        said = _about(verdict["warnings"], "'decide'", "`approved`", "state.review", "'ask_plain'")
        assert len(said) == 1, verdict["warnings"]
        assert not _about(verdict["issues"], "`approved`"), verdict["issues"]

    def test_both_arms_asking_it_is_never_named(self):
        verdict = _validate_orchestration(_orch("cond", _if("ask_signoff", "ask_plain"),
                                                _human("ask_signoff", "decide", "approved"),
                                                _human("ask_plain", "decide", "approved"), _decide()))
        assert not _about(verdict["warnings"] + verdict["issues"], "`approved`")

    def test_a_read_inside_the_arm_that_asks_it_is_never_named(self):
        verdict = _validate_orchestration(_orch("cond", _if("ask_signoff", "ask_plain"),
                                                _human("ask_signoff", "decide", "approved"),
                                                _human("ask_plain", "end"), _decide()))
        assert not _about(verdict["warnings"] + verdict["issues"], "`approved`")


# ── a writer that cannot reach the read no longer switches the rules off ─────────────────────────────────────────

class TestAWriterThatCannotReachTheRead:
    def test_no_longer_switches_the_field_rule_off(self):
        fixup = {"id": "fixup", "name": "fixup", "type": "transform", "transform_code": "result = {'approved': True}",
                 "output_key": "review", "next_step_id": "end"}
        verdict = _validate_orchestration(_orch("ask", _human("ask", "decide"), _decide("fixup"), fixup))
        said = _about(verdict["issues"], "Step 'decide' reads `approved` from state.review, which human step 'ask' "
                                         "never writes")
        assert len(said) == 1, verdict["issues"]

    def test_one_that_reaches_it_still_does(self):
        fixup = {"id": "fixup", "name": "fixup", "type": "transform", "transform_code": "result = {'approved': True}",
                 "output_key": "review", "next_step_id": "decide"}
        verdict = _validate_orchestration(_orch("ask", _human("ask", "fixup"), fixup, _decide()))
        assert not _about(verdict["issues"] + verdict["warnings"], "`approved`")


# ── a Loop over a field a reaching writer declares as no list ─────────────────────────────────────────────────────

def _model(sid: str, kind: str) -> dict:
    return {"id": sid, "name": sid, "type": "llm", "prompt_template": "List the keywords.", "output_key": "k",
            "next_step_id": "each", "output_fields": [{"name": "items", "type": kind}]}


LOOP = {"id": "each", "name": "each", "type": "loop", "loop_over": "state.k.items", "loop_step_ids": ["one"],
        "next_step_id": "end"}
ONE = {"id": "one", "name": "one", "type": "print", "print_content": "{{ state.loop_item }}", "output_key": "o"}


class TestALoopOverAFieldOneWayDeclaresNoList:
    def test_is_refused_naming_that_writer(self):
        verdict = _validate_orchestration(_orch("cond", _if("plain", "listed"), _model("plain", "text"),
                                                _model("listed", "list"), LOOP, ONE))
        said = _about(verdict["issues"], "Loop 'each' goes through state.k.items", "'plain'", "not a list")
        assert len(said) == 1, verdict["issues"]

    def test_both_ways_declaring_a_list_pass(self):
        verdict = _validate_orchestration(_orch("cond", _if("plain", "listed"), _model("plain", "list"),
                                                _model("listed", "list"), LOOP, ONE))
        assert not _about(verdict["issues"], "Loop 'each'")


# ── two Tool steps on two arms writing one key ────────────────────────────────────────────────────────────────────

FINDER = {"name": "find_leads", "does": "Finds leads", "outputSchema": json_schema(
    [{"name": "url", "type": "string"}, {"name": "title", "type": "string"}])}
BRIEF = {"name": "brief_leads", "does": "Finds leads briefly", "outputSchema": json_schema(
    [{"name": "url", "type": "string"}])}
TOOLS = step_answers.tool_answers([FINDER, BRIEF], {"find_leads", "brief_leads"})


def _tool(sid: str, name: str, nxt: str) -> dict:
    return {"id": sid, "name": sid, "type": "tool", "forced_tool": name, "output_key": "found", "next_step_id": nxt}


def _show(sid: str, field: str, nxt: str = "end") -> dict:
    return {"id": sid, "name": sid, "type": "print", "print_content": f"{{{{ state.found.{field} }}}}",
            "output_key": f"o_{sid}", "next_step_id": nxt}


class TestTwoToolsOnTwoArms:
    def test_a_field_only_one_answers_is_a_warning_after_the_join(self):
        steps = [_if("finder", "brief"), _tool("finder", "find_leads", "show"), _tool("brief", "brief_leads", "show"),
                 _show("show", "title")]
        verdict = _validate_orchestration(_orch("cond", *steps), tool_answers=TOOLS)
        assert len(_about(verdict["warnings"], "'show'", "`title`", "state.found", "'brief'")) == 1, verdict
        assert not _about(verdict["issues"], "`title`"), verdict["issues"]

    def test_a_read_only_one_tool_reaches_is_held_to_its_declaration(self):
        steps = [_if("finder", "brief"), _tool("finder", "find_leads", "end"), _tool("brief", "brief_leads", "show"),
                 _show("show", "title")]
        issues, _ = _tool_answer_reads(steps, TOOLS)
        assert len(_about(issues, "title")) == 1, issues

    def test_both_answering_it_is_never_named(self):
        both = step_answers.tool_answers([FINDER, {**BRIEF, "outputSchema": FINDER["outputSchema"]}],
                                         {"find_leads", "brief_leads"})
        steps = [_if("finder", "brief"), _tool("finder", "find_leads", "show"), _tool("brief", "brief_leads", "show"),
                 _show("show", "title")]
        verdict = _validate_orchestration(_orch("cond", *steps), tool_answers=both)
        assert not _about(verdict["warnings"] + verdict["issues"], "`title`")


# ── an Ingest step reached with its records' key unset ────────────────────────────────────────────────────────────

def _store() -> dict:
    return {"id": "store", "name": "store", "type": "ingest", "ingest": {"kb_id": "kb_notes", "records_from": "records"},
            "output_key": "stored", "next_step_id": "end"}


def _make(sid: str = "make") -> dict:
    return {"id": sid, "name": sid, "type": "transform", "transform_code": "result = [{'text': 'a note'}]",
            "output_key": "records", "next_step_id": "store"}


class TestAnIngestReachedWithItsRecordsUnset:
    def test_is_refused_naming_the_way(self):
        issues = _validate_orchestration(_orch("cond", _if("make", "store"), _make(), _store()))["issues"]
        assert len(_about(issues, "'store'", "state.records", "false branch")) == 1, issues

    def test_written_every_way_or_a_default_passes(self):
        both = _validate_orchestration(_orch("cond", _if("make", "again"), _make(), _make("again"), _store()))
        assert not _about(both["issues"], "state.records"), both["issues"]
        seeded = _validate_orchestration(_orch("cond", _if("make", "store"), _make(), _store(),
                                               schema={"records": {"type": "list", "default": []}}))
        assert not _about(seeded["issues"], "state.records"), seeded["issues"]


# ── an input_key read before any step writes it ───────────────────────────────────────────────────────────────────

def _answer(nxt: str) -> dict:
    return {"id": "answer", "name": "answer", "type": "llm", "prompt_template": "Answer the customer.",
            "input_keys": ["notes"], "output_key": "reply", "next_step_id": nxt}


NOTES = {"id": "note", "name": "note", "type": "transform", "transform_code": "result = 'n'", "output_key": "notes",
         "next_step_id": "end"}


class TestAnInputKeyReadBeforeItsWriter:
    def test_is_a_warning(self):
        verdict = _validate_orchestration(_orch("answer", _answer("note"), NOTES))
        assert len(_about(verdict["warnings"], "'answer'", "'notes'", "before any step writes it")) == 1, verdict
        assert not _about(verdict["issues"], "'notes'"), verdict["issues"]

    def test_written_before_or_declared_is_never_named(self):
        before = _validate_orchestration(_orch("note", {**NOTES, "next_step_id": "answer"}, _answer("end")))
        assert not _about(before["warnings"] + before["issues"], "'notes'")
        declared = _validate_orchestration(_orch("answer", _answer("end"), schema={"notes": {"type": "str"}}))
        assert not _about(declared["warnings"], "'notes'")

    def test_one_no_step_writes_keeps_its_issue_and_gets_no_warning(self):
        verdict = _validate_orchestration(_orch("answer", _answer("end")))
        assert len(_about(verdict["issues"], "reads input_key 'notes', which nothing writes")) == 1
        assert not _about(verdict["warnings"], "'notes'")
