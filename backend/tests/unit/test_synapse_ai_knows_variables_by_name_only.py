"""Synapse AI knows the workspace's variables by name, never by value (F1271, REACH1; D340 §6, Naveen 10-30).

A plan uses a variable as `{{ global.x }}`, so the planner needs its name, never what it holds. `list_variables`
gave the first 40 characters of every value that was not held as a secret: a token saved as a plain variable, or a
customer's address, reached the model and the builder's tool result. These pin:

- `list_variables` answers each variable's name and the reference that uses it, and nothing of its value;
- `read_workspace` topic `variables` is the same, for gather to take (D0), and says where they are made when there
  are none;
- the census's Variables row names the topic, and says adding or changing one is the screen's.
"""
from __future__ import annotations

import json

import pytest

from core.builder_tools import execute_builder_tool, execute_builder_tool_with_events

PLAIN = "eu-west-1-plain-value-for-the-region"
TOKEN_LIKE = "sk-" + "live-" + "Z" * 60


@pytest.fixture
async def saved():
    from core import variables

    await variables.save({"REGION": PLAIN, "BILLING_TOKEN": TOKEN_LIKE})
    yield
    await variables.save({})


def _no_value_in(text: str) -> None:
    for value in (PLAIN, TOKEN_LIKE):
        assert value[:6] not in text, f"a value's start reached the builder: {text}"


class TestListVariables:
    async def test_it_answers_names_and_references_only(self, saved):
        raw = await execute_builder_tool("list_variables", {}, None)
        listed = json.loads(raw)
        assert listed == [
            {"key": "BILLING_TOKEN", "reference": "{{ global.BILLING_TOKEN }}"},
            {"key": "REGION", "reference": "{{ global.REGION }}"},
        ]
        _no_value_in(raw)

    def test_its_description_says_names_only(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS

        schema = next(t for t in BUILDER_TOOL_SCHEMAS if t["function"]["name"] == "list_variables")
        said = schema["function"]["description"]
        assert "preview" not in said.lower() and "never" in said.lower()


class TestTheVariablesTopic:
    async def _read(self, **args) -> dict:
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "variables", **args},
                                                        server_module=None)
        _no_value_in(raw)
        return json.loads(raw)

    async def test_it_names_each_variable_and_its_reference(self, saved):
        got = await self._read()
        assert got["items"] == [
            {"name": "BILLING_TOKEN", "reference": "{{ global.BILLING_TOKEN }}"},
            {"name": "REGION", "reference": "{{ global.REGION }}"},
        ]
        assert "never" in got["note"].lower()

    async def test_a_name_narrows_it(self, saved):
        got = await self._read(name="region")
        assert [i["name"] for i in got["items"]] == ["REGION"]

    async def test_none_says_where_they_are_made(self):
        from core import variables

        await variables.save({})
        got = await self._read()
        assert got["items"] == [] and "Settings › Variables" in got["note"]


def test_the_census_row_reaches_the_names_and_links_the_rest():
    from core.feature_census import CENSUS

    row = next(s for s in CENSUS if s.id == "variables")
    assert "read_workspace:variables" in row.tools and "list_variables" in row.tools
    assert "value" in row.does and "adding or changing" in row.cant
