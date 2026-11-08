"""A plan's code reads the answer the step before it really gets (F1394; BLD1's media dry run on 5d99d49d, Sonnet 4.6
via the CLI; Naveen 11-05: "Both in v2.0", and the restated plan: a field check at the plan check, said once a run).

"Every morning … patio in Porto … find a free forecast service": the planner picked Open-Meteo with a right URL and
fetched it with `scrape_url`, then a Transform ran `daily = data['daily']` on its answer, and the run failed with a
KeyError. `scrape_url` answers `{success, url, markdown}`: the API's JSON was text inside `markdown`. The plan check now
reads the fields a code step, a condition or a template looks up in a Tool step's answer, and when the tool's answer is
known (the built-in page reads' own shapes, or a tool's declared `outputSchema`) and has no such field, the plan goes
back once a run with the fields it has and the two ways on: read the field it has, or call a JSON API with a GET tool.
A field the answer has, a tool whose answer is not known, and code that parses the answer as text all pass.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

URL = ("https://api.open-meteo.com/v1/forecast?latitude=41.15&longitude=-8.61"
       "&daily=temperature_2m_max,precipitation_probability_max&timezone=Europe/Lisbon&forecast_days=2")
FETCH = {"id": "fetch_forecast", "type": "tool", "name": "Fetch tomorrow's forecast", "tool": "scrape_url",
         "does": "Reads Open-Meteo's forecast for Porto", "config": {"tool_args": {"url": URL}}, "next": "parse"}
PORTO = """\
data = state.fetch_forecast
daily = data['daily']
temp = daily['temperature_2m_max'][1]
rain = daily['precipitation_probability_max'][1]
result = {'temp': temp, 'rain': rain, 'patio': temp >= 18 and rain < 30}
"""


def _code(code: str, nxt: str = "decide") -> dict:
    return {"id": "parse", "type": "transform", "name": "Parse Tomorrow's Values", "does": "Reads tomorrow's values",
            "config": {"transform_code": code}, "next": nxt}


DECIDE = {"id": "decide", "type": "if_else", "name": "Patio weather?", "does": "Checks the patio weather",
          "condition": "state.parse.patio == True", "branches": {"true": "say_yes", "false": "say_no"}}
SAY_YES = {"id": "say_yes", "type": "llm", "name": "Say yes", "does": "Writes the patio's yes", "agent": "Writer",
           "prompt": "Tell the team the patio is open tomorrow.", "next": "end"}
SAY_NO = {"id": "say_no", "type": "llm", "name": "Say no", "does": "Writes the patio's no", "agent": "Writer",
          "prompt": "Tell the team the patio stays closed tomorrow.", "next": "end"}


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "Porto patio", "summary": "Every morning, whether the patio opens tomorrow.",
            "steps": list(steps), "agents": [{"name": "Writer", "purpose": "Writes short notes for the team."}]}


def _ws(*tools: dict) -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": "scrape_url", "does": "Reads a web page"}, *tools])


async def _said(plan: dict, ws: build_plan.Workspace | None = None, state: dict | None = None) -> list[str]:
    found = await build_plan.examine(plan, None, ws=ws or _ws(), state={"user_message": "x"} if state is None
                                     else state)
    return [e for e in found.errors if "an answer with no such field" in e]


class TestSentBack:
    async def test_the_porto_plan_reads_a_field_scrape_urls_answer_does_not_have(self):
        said = await _said(_plan(FETCH, _code(PORTO), DECIDE, SAY_YES, SAY_NO))
        assert len(said) == 1, said
        line = said[0]
        assert line.startswith("step parse: its code reads `daily` from `state.fetch_forecast`, an answer with no "
                               "such field: step fetch_forecast calls `scrape_url`, which answers an object with "
                               "`success`, `url`, `markdown`"), line
        # The two ways on, both named: the field it has, or a GET tool for a JSON API.
        assert "`markdown`" in line and "parse" in line.split("which answers", 1)[1]
        assert "GET tool" in line and "looked up" in line

    @pytest.mark.parametrize("code", [
        "result = state.fetch_forecast['daily']['temperature_2m_max'][1]",
        "result = state['fetch_forecast'].get('daily')",
        "result = state.fetch_forecast.daily",
        # Parsed first: `json.loads` of the stored answer is the answer itself, so `daily` is still not in it. This
        # was left alone as "read as text", the hero build's own failure (F1404; Naveen 11-06, "move the pin").
        "import json\nresult = json.loads(state.fetch_forecast)['daily']",
    ])
    async def test_every_way_code_looks_a_field_up(self, code):
        said = await _said(_plan(FETCH, _code(code, "end")))
        assert len(said) == 1 and "reads `daily` from `state.fetch_forecast`" in said[0], said

    async def test_a_condition_reading_a_field_its_answer_has_not(self):
        cond = {**DECIDE, "condition": "state.fetch_forecast.daily is not None"}
        said = await _said(_plan({**FETCH, "next": "decide"}, cond, SAY_YES, SAY_NO))
        assert len(said) == 1 and said[0].startswith("step decide: its condition reads `daily` from "
                                                     "`state.fetch_forecast`"), said

    async def test_a_declared_output_its_code_reads_past(self):
        """A tool here that declares what it answers (`outputSchema`) is held to it as the built-ins are."""
        tool = {"name": "forecast_lookup", "does": "Gets a forecast",
                "outputSchema": {"type": "object", "properties": {"hourly": {"type": "object"}}}}
        fetch = {**FETCH, "tool": "forecast_lookup"}
        said = await _said(_plan(fetch, _code(PORTO)), ws=_ws(tool))
        assert len(said) == 1 and "`forecast_lookup`, which answers an object with `hourly`" in said[0], said

    async def test_said_once_a_run(self):
        state = {"user_message": "x"}
        assert len(await _said(_plan(FETCH, _code(PORTO), DECIDE, SAY_YES, SAY_NO), state=state)) == 1
        assert await _said(_plan(FETCH, _code(PORTO), DECIDE, SAY_YES, SAY_NO), state=state) == []


class TestLeftAlone:
    @pytest.mark.parametrize("code", [
        # Reading the field it has, and parsing the API's JSON out of it.
        "import json\nraw = state.fetch_forecast['markdown'].strip().strip('`')\n"
        "result = json.loads(raw)['daily']['temperature_2m_max'][1]",
        "result = state.fetch_forecast.get('links') or []",
        "result = state.fetch_forecast.success",
        # The whole answer handed on.
        "result = state.fetch_forecast",
        # A step's own `.get` on another key; the run's own message.
        "result = state.user_input.strip()",
    ])
    async def test_a_field_the_answer_has_or_no_field_at_all(self, code):
        assert await _said(_plan(FETCH, _code(code, "end"))) == []

    async def test_a_tool_whose_answer_is_not_known(self):
        tool = {"name": "forecast_lookup", "does": "Gets a forecast"}
        assert await _said(_plan({**FETCH, "tool": "forecast_lookup"}, _code(PORTO, "end")), ws=_ws(tool)) == []

    async def test_a_declared_output_with_the_field(self):
        tool = {"name": "forecast_lookup", "does": "Gets a forecast",
                "outputSchema": {"type": "object", "properties": {"daily": {"type": "object"}}}}
        assert await _said(_plan({**FETCH, "tool": "forecast_lookup"}, _code(PORTO, "end")), ws=_ws(tool)) == []

    async def test_a_code_steps_own_answer_is_not_held(self):
        """What a Transform answers is its own code's: `state.parse.patio` is read, not checked."""
        assert await _said(_plan({**FETCH, "next": "parse"},
                                 _code("result = {'patio': True}"), DECIDE, SAY_YES, SAY_NO)) == []


def test_the_built_in_page_reads_shapes_are_what_the_scraper_answers():
    """The shapes are the scraper's own response keys, read from its source, so a key it adds is never refused."""
    import ast
    from pathlib import Path

    from core.native_builder import structure

    src = Path(__file__).resolve().parents[2] / "tools" / "web_scraper.py"
    tree = ast.parse(src.read_text())
    handlers = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    for tool, shape in structure.ANSWERS.items():
        fn = handlers[f"_handle_{tool}"]
        keys = {k.value for d in ast.walk(fn) if isinstance(d, ast.Dict) for k in d.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        keys |= {t.slice.value for t in ast.walk(fn) if isinstance(t, ast.Subscript)
                 and isinstance(t.value, ast.Name) and t.value.id == "response" and isinstance(t.slice, ast.Constant)}
        assert {"success", "url"} <= keys, tool
        assert keys - {"error"} <= set(shape), (tool, sorted(keys - set(shape)))
