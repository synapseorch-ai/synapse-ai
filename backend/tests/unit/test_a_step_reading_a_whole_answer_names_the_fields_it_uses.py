"""A step that reads a tool's whole object answer names the fields of it that it uses, or its use is said as
unchecked; a field it writes as code is held too (F1705).

Asked for the humidity, a plan read a weather tool's whole answer in an llm step and assumed in plain words that the
tool "returns humidity data", though the tool declares only its city and temperature. A step that names the fields it
uses (`uses_fields`, F1675) is held to the declaration; one that names none was not checked at all, and nothing can
tell from plain words which field it means.

Now an llm or agent step that reads the whole answer of a tool step whose tool declares an object answer must name the
fields it uses: a plan without them goes back once a run, and if the next plan still names none, its card says that
step's use of the answer isn't checked. And a field the step's prompt writes as code, a dotted path or a backticked
name, is held to the declaration like one in `uses_fields`.

Near misses: a list answer, a tool that declares no shape, and a page read's own answer are not asked; a step with
`uses_fields` goes through the existing check; a plain word in the prompt is never read as a field.
"""
from __future__ import annotations

from core.native_builder import build_plan
from core.orchestration.output_fields import json_schema

OBJECT = json_schema([{"name": "city", "type": "string"}, {"name": "temp_c", "type": "number"}])
WS = build_plan.Workspace(tools=[
    {"name": "porto_weather", "does": "The weather in Porto", "outputSchema": OBJECT, "answer_owner": "custom"},
    {"name": "list_rows", "does": "Rows", "outputSchema": {"type": "array", "items": {"type": "object"}},
     "answer_owner": "custom"},
    {"name": "loose_tool", "does": "Says something"},
    {"name": "scrape_url", "does": "Reads a web page"}])


def _plan(prompt: str, *, tool: str = "porto_weather", uses: list[str] | None = None, kind: str = "llm") -> dict:
    say = {"id": "say", "type": kind, "name": "Say it", "does": "Reports the weather", "prompt": prompt, "next": "end"}
    if kind == "agent":
        say["agent"] = "Reporter"
    if uses is not None:
        say["uses_fields"] = uses
    draft = {"kind": "create", "title": "Weather", "summary": "Reports the weather.",
             "steps": [{"id": "find", "type": "tool", "name": "Get the weather", "does": "Gets the weather",
                        "tool": tool, "next": "say"}, say]}
    if kind == "agent":
        draft["agents"] = [{"name": "Reporter", "purpose": "Reports the weather"}]
    return draft


async def _examined(draft: dict, state: dict | None = None):
    return await build_plan.examine(draft, None, ws=WS, state=state if state is not None else {"user_message": "x"})


def _of(lines: list[str], word: str) -> list[str]:
    return [line for line in lines if word in line]


WHOLE = "Say whether it is humid enough for the patio: {{ state.find }}"


class TestAWholeAnswerWithNoFieldsNamed:
    async def test_goes_back_once_then_is_said_as_unchecked(self):
        state = {"user_message": "x"}
        [said] = _of((await _examined(_plan(WHOLE), state)).errors, "uses_fields")
        assert "step say" in said and "`porto_weather`" in said and "`city`, `temp_c`" in said, said
        second = await _examined(_plan(WHOLE), state)
        assert _of(second.errors, "uses_fields") == [], "sent back once a run, never a loop"
        assert _of(second.notes, "isn't checked"), second.notes

    async def test_an_agent_step_too(self):
        assert _of((await _examined(_plan(WHOLE, kind="agent"))).errors, "uses_fields")

    async def test_input_keys_read_it_whole_too(self):
        draft = _plan("Say whether it is humid enough for the patio.")
        draft["steps"][1]["config"] = {"input_keys": ["find"]}
        assert _of((await _examined(draft)).errors, "uses_fields")


class TestNearMisses:
    async def test_a_step_that_names_its_fields_is_not_asked(self):
        found = await _examined(_plan(WHOLE, uses=["find.temp_c"]))
        assert _of(found.errors, "uses_fields") == [] and _of(found.notes, "isn't checked") == []

    async def test_a_list_answer_is_not_asked(self):
        assert _of((await _examined(_plan(WHOLE, tool="list_rows"))).errors, "uses_fields") == []

    async def test_a_tool_that_declares_no_shape_is_not_asked(self):
        assert _of((await _examined(_plan(WHOLE, tool="loose_tool"))).errors, "uses_fields") == []

    async def test_a_page_read_is_not_asked(self):
        assert _of((await _examined(_plan(WHOLE, tool="scrape_url"))).errors, "uses_fields") == []

    async def test_a_field_read_by_its_path_is_not_asked(self):
        assert _of((await _examined(_plan("It is {{ state.find.temp_c }} degrees."))).errors, "uses_fields") == []


class TestAFieldWrittenAsCode:
    async def test_a_backticked_name_the_answer_lacks_goes_back(self):
        found = await _examined(_plan("Report the `humidity` from {{ state.find }}.", uses=["find.temp_c"]))
        assert _of(found.errors, "`humidity`"), found.errors

    async def test_a_dotted_path_the_answer_lacks_goes_back(self):
        found = await _examined(_plan("Read find.humidity in {{ state.find }}.", uses=["find.temp_c"]))
        assert _of(found.errors, "`humidity`"), found.errors

    async def test_a_plain_word_is_never_read_as_a_field(self):
        found = await _examined(_plan("Report the humidity from {{ state.find }}.", uses=["find.temp_c"]))
        assert _of(found.errors, "humidity") == [], found.errors

    async def test_a_backticked_name_the_answer_has_is_silent(self):
        found = await _examined(_plan("Report `temp_c` from {{ state.find }}.", uses=["find.temp_c"]))
        assert _of(found.errors, "temp_c") == [], found.errors
