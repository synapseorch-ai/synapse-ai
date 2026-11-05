"""A plan's code that parses a Tool step's answer with `json.loads` reads the same answer (F1404; BLD1's media rig on
v2.0 49e1f13b, Sonnet 4.6 via the CLI; Naveen 11-06: "Build, move the pin").

The hero build, "Porto's patio forecast": the plan fetched Open-Meteo with `scrape_url`, and its Transform ran
`data = json.loads(state['get_forecast'])`, then `data['daily']`, and the first run failed with KeyError 'daily'.
A Tool step stores its answer as text, the answer's JSON, so `json.loads` gives back `scrape_url`'s answer itself,
`{success, url, markdown, …}`, with the API's JSON as text inside `markdown`. F1394's check reads the fields code
looks up in a known answer, but followed a name only when it was assigned straight from `state`, so the parse hid
the read. Now a name assigned from `json.loads(...)` of the answer (or of a name holding it), and an inline
`json.loads(state.x)['f']`, are read as the answer, and the plan goes back with F1394's words, said as they are.
`json.loads` of a field of the answer (its `markdown`) is that field's own JSON, never the answer, and passes.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

URL = ("https://api.open-meteo.com/v1/forecast?latitude=41.1579&longitude=-8.6291"
       "&daily=precipitation_probability_max,wind_speed_10m_max&timezone=Europe%2FLisbon&forecast_days=2")
FETCH = {"id": "get_forecast", "type": "tool", "name": "Fetch Tomorrow's Forecast", "tool": "scrape_url",
         "does": "Reads Open-Meteo's forecast for Porto", "config": {"tool_args": {"url": URL}}, "next": "decide"}
#: The hero's own Transform, as the rig's build saved it (orch_tu6tecb, step `decide`).
HERO = """\
import json
data = json.loads(state['get_forecast'])
# index 1 is tomorrow
rain_pct = data['daily']['precipitation_probability_max'][1]
wind_kmh = data['daily']['wind_speed_10m_max'][1]
date = data['daily']['time'][1]
if rain_pct > 40 or wind_kmh > 30:
    verdict = 'NO — keep the tables in'
    reason_parts = []
    if rain_pct > 40:
        reason_parts.append(f'{rain_pct}% chance of rain (limit 40%)')
    if wind_kmh > 30:
        reason_parts.append(f'wind up to {wind_kmh} km/h (limit 30 km/h)')
    reason = ' and '.join(reason_parts)
else:
    verdict = 'YES — put the tables out'
    reason = f'{rain_pct}% chance of rain, wind up to {wind_kmh} km/h'
result = f'Harborpine Coffee patio — {date}\\n\\n{verdict}\\n({reason})'
"""
NOTIFY = {"id": "notify", "type": "llm", "name": "Format the Message", "does": "Writes the morning briefing",
          "agent": "Writer", "prompt": "Turn this into a friendly briefing:\n\n{{ state.decide }}", "next": "end"}


def _code(code: str) -> dict:
    return {"id": "decide", "type": "transform", "name": "Check the Rules", "does": "Checks tomorrow against the rules",
            "config": {"transform_code": code}, "next": "notify"}


def _plan(code: str) -> dict:
    return {"kind": "create", "title": "Patio Tables Morning Check",
            "summary": "Every morning, whether the patio tables go out.", "steps": [FETCH, _code(code), NOTIFY],
            "agents": [{"name": "Writer", "purpose": "Writes short notes for the café."}]}


async def _said(code: str) -> list[str]:
    ws = build_plan.Workspace(tools=[{"name": "scrape_url", "does": "Reads a web page"}])
    found = await build_plan.examine(_plan(code), None, ws=ws, state={"user_message": "x"})
    return [e for e in found.errors if "an answer with no such field" in e]


class TestSentBack:
    async def test_the_hero_transform_reads_daily_from_scrape_urls_answer(self):
        said = await _said(HERO)
        assert len(said) == 1, said
        assert said[0].startswith("step decide: its code reads `daily` from `state.get_forecast`, an answer with no "
                                  "such field: step get_forecast calls `scrape_url`, which answers an object with "
                                  "`success`, `url`, `markdown`"), said[0]
        # F1394's words, as they are: the field it has, parsed there, or a GET tool for a JSON API.
        assert "a page's text is in `markdown`, and so is an API's JSON fetched as a page: parse it there" in said[0]
        assert "GET tool" in said[0]

    @pytest.mark.parametrize("code", [
        "import json\ndata = json.loads(state['get_forecast'])\nresult = data['daily']",
        "import json\ndata = json.loads(state.get_forecast)\nresult = data.get('daily')",
        "import json\ndata = json.loads(state.get('get_forecast'))\nresult = data['daily']['time']",
        # Through a name holding the answer.
        "import json\nraw = state['get_forecast']\ndata = json.loads(raw)\nresult = data['daily']",
        # Inline, on the answer or on a name holding it.
        "import json\nresult = json.loads(state['get_forecast'])['daily']",
        "import json\nraw = state.get_forecast\nresult = json.loads(raw).get('daily')",
        # json's own `loads`, imported by name.
        "from json import loads\ndata = loads(state['get_forecast'])\nresult = data['daily']",
    ])
    async def test_every_way_the_answer_is_parsed_then_read(self, code):
        said = await _said(code)
        assert len(said) == 1 and "reads `daily` from `state.get_forecast`" in said[0], said


class TestLeftAlone:
    @pytest.mark.parametrize("code", [
        # The API's JSON parsed out of the field it is in: its fields are that JSON's, never the answer's.
        "import json\ndata = json.loads(state['get_forecast']['markdown'].strip().strip('`'))\n"
        "result = data['daily']['time'][1]",
        "import json\nwrapper = json.loads(state['get_forecast'])\n"
        "data = json.loads(wrapper['markdown'].strip().strip('`'))\nresult = data['daily']['time'][1]",
        "import json\nresult = json.loads(json.loads(state.get_forecast)['markdown'].strip('`'))['daily']",
        # Fields the answer has.
        "import json\ndata = json.loads(state['get_forecast'])\nresult = data['success'] and data['markdown']",
        # Another key, and code that reads no answer.
        "import json\ndata = json.loads(state['user_input'])\nresult = data['daily']",
        "result = {'patio': True}",
        # A `loads` that is not json's.
        "from pickle import loads\ndata = loads(state['get_forecast'])\nresult = data['daily']",
    ])
    async def test_a_field_the_answer_has_or_no_read_of_it(self, code):
        assert await _said(code) == []
