"""A template over the run's state has one form, `{{ state.x }}` (F1252; Naveen 10-30; TRI1's triage, part 2, S7).

There were two. `{state.x}` was the engine's first, for a step's prompt; `{{ … }}` came with HTTP tools, agents'
prompts and global variables (4520c514, 09-03), and `{{ state.x }}` was added beside the old one without converting
it. Each renderer then read whichever existed when it was written: a Human step and a Print read only `{state.x}`, so
the posts a plan wrote with `{{state.…}}` reached Naveen as `{…}`. Now there is one: `templating.STATE_REF`. An older
`{state.x}` in a step's templates is written `{{ state.x }}` wherever a definition is read or written (the step
model, the store's save and load, so an import, an export and the API's read too): a workflow saved before keeps
working, with nothing re-planned. The converter is idempotent and leaves `{{ … }}`, JSON, code and conditions alone.
"""
from __future__ import annotations

import pytest

from core import templating
from core.models_orchestration import Orchestration, StepConfig
from core.templating import STATE_REF, one_form, one_form_definition


class TestTheConverter:
    @pytest.mark.parametrize("old, new", [
        ("{state.user_input}", "{{ state.user_input }}"),
        ("Order {state.ask.order} for {state.user_input}.", "Order {{ state.ask.order }} for {{ state.user_input }}."),
        ("{state.loop_item}", "{{ state.loop_item }}"),
    ])
    def test_the_older_form_is_written_in_the_one_form(self, old, new):
        assert one_form(old) == new

    @pytest.mark.parametrize("text", [
        "{{state.user_input}}", "{{ state.user_input }}", "{{ global.api_base }}", "{{ body.data }}",
        '{"state": {"order": 1}}', '{"text": "Today: {{state.check_weather}}"}', "{state}", "{ state.x }",
        "no reference here", "", "{param} and {data.key} in a URL",
    ])
    def test_everything_else_is_left_as_it_is(self, text):
        assert one_form(text) == text

    @pytest.mark.parametrize("text", ["{state.a}", "x {state.a.b} {{state.c}} {\"state\": 1}", "{state.a} {state.a}"])
    def test_twice_is_once(self, text):
        assert one_form(one_form(text)) == one_form(text)

    def test_the_one_form_is_what_state_ref_reads(self):
        assert STATE_REF.findall(one_form("{state.a.b} and {state.c}")) == ["a.b", "c"]
        assert STATE_REF.findall("{{state.a}} {{ state.b.c }}") == ["a", "b.c"]
        assert not STATE_REF.findall("{state.a} {{ global.x }}")


def _step(**fields) -> dict:
    return {"id": "s", "name": "S", "type": "llm", **fields}


class TestEveryTemplateOfAStep:
    def test_the_step_model_writes_them_in_the_one_form(self):
        step = StepConfig.model_validate(_step(
            prompt_template="Summarise {state.user_input}", evaluator_prompt="Judge {state.draft}"))
        assert step.prompt_template == "Summarise {{ state.user_input }}"
        assert step.evaluator_prompt == "Judge {{ state.draft }}"

    @pytest.mark.parametrize("fields, key, want", [
        ({"type": "print", "print_content": "Done: {state.summary}"}, "print_content", "Done: {{ state.summary }}"),
        ({"type": "human", "human_prompt": "Check {state.post.text}"}, "human_prompt", "Check {{ state.post.text }}"),
        ({"type": "tool", "forced_tool": "t", "tool_arguments": {"q": "{state.q}", "deep": [{"x": "{state.a.b}"}]}},
         "tool_arguments", {"q": "{{ state.q }}", "deep": [{"x": "{{ state.a.b }}"}]}),
        ({"type": "browser", "recording_id": "rec_x", "recording_inputs": {"order number": "{state.user_input}"}},
         "recording_inputs", {"order number": "{{ state.user_input }}"}),
        ({"type": "retrieve", "retrieve": {"kb_ids": ["kb_1"], "query": "{state.user_input}"}},
         "retrieve", {"kb_ids": ["kb_1"], "query": "{{ state.user_input }}"}),
    ])
    def test_each_kind_of_template(self, fields, key, want):
        assert getattr(StepConfig.model_validate(_step(**fields)), key) == want

    def test_code_and_conditions_are_never_templates(self):
        code = "result = f\"{state.user_input}\"  # Python, not a template"
        step = StepConfig.model_validate(_step(type="transform", transform_code=code))
        assert step.transform_code == code
        cond = StepConfig.model_validate(_step(type="if_else", if_condition="state.ok == True"))
        assert cond.if_condition == "state.ok == True"

    def test_a_whole_definition(self):
        orch = Orchestration.model_validate({"id": "o", "name": "O", "entry_step_id": "s",
                                             "steps": [_step(prompt_template="{state.user_input}")]})
        assert orch.steps[0].prompt_template == "{{ state.user_input }}"
        doc = one_form_definition({"id": "o", "steps": [_step(prompt_template="{state.x}")], "name": "O"})
        assert doc["steps"][0]["prompt_template"] == "{{ state.x }}" and doc["name"] == "O"


class TestTheStore:
    async def test_a_save_is_in_the_one_form(self):
        from core.store.resources import get_orchestration, save_orchestration

        await save_orchestration({"id": "orch_f1252", "name": "One form", "entry_step_id": "s",
                                  "steps": [_step(prompt_template="Hello {state.user_input}")]})
        assert (await get_orchestration("orch_f1252"))["steps"][0]["prompt_template"] == "Hello {{ state.user_input }}"

    async def test_a_row_saved_before_is_read_in_the_one_form(self, monkeypatch):
        from core.store.resources import get_orchestration, load_orchestrations, save_orchestration

        with monkeypatch.context() as m:
            m.setattr(templating, "one_form_definition", lambda doc: doc)  # as a row written before F1252
            await save_orchestration({"id": "orch_old", "name": "Old", "entry_step_id": "s",
                                      "steps": [_step(type="print", print_content="Post: {state.post}")]})
        assert (await get_orchestration("orch_old"))["steps"][0]["print_content"] == "Post: {{ state.post }}"
        [old] = [o for o in await load_orchestrations() if o["id"] == "orch_old"]
        assert old["steps"][0]["print_content"] == "Post: {{ state.post }}"


async def test_an_older_bundle_imports_and_its_export_is_in_the_one_form(client):
    import json

    bundle = {"version": 1, "orchestrations": [{"id": "orch_pack", "name": "Pack", "entry_step_id": "s",
                                                "steps": [_step(type="human", human_prompt="Approve {state.draft}?")]}],
              "agents": [], "custom_tools": [], "mcp_servers": []}
    got = await client.post("/api/import", json={"bundle": bundle, "selected_orchestration_ids": ["orch_pack"]})
    assert got.status_code < 300, got.text
    from core.store.resources import load_orchestrations

    [orch] = [o for o in await load_orchestrations() if o.get("name") == "Pack"]
    assert orch["steps"][0]["human_prompt"] == "Approve {{ state.draft }}?"
    out = await client.post("/api/export", json={"orchestration_ids": [orch["id"]], "agent_ids": [],
                                                                "custom_tool_names": [], "mcp_server_names": []})
    assert out.status_code < 300, out.text
    assert "{state." not in json.dumps(out.json()) and "{{ state.draft }}" in json.dumps(out.json())
