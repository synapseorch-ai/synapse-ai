"""A tool made from a curl takes a value that changes from call to call as an input, and a skill is read before it is
changed (F1377, F1379, TRI1; CAP3's ff18-f2 fix-01; BLD1's v2.0 shots; Naveen 11-04: "Build with the code check").

- F1377 (fix-01): fixing an HTTP tool from a pasted curl, the builder kept the curl's address as written, one order's
  id in its path, so the tool fetched that one order every time. Rule 23 and update_http_tool's `url` say a value in
  the path or query that belongs to one call (an id, a date, a name) becomes an input, unless the person says the
  address stays the same for every call. And code checks it: a tool made or changed `from` a curl whose address keeps
  such a value as written (`http_facts.one_calls_value`: a number of two digits or more, a UUID, a date) is sent back
  once; sent again as it is, it is taken.
- F1379 (BLD1's shop-hours pass): asked to change one line of a skill, Synapse AI asked the person to paste the skill,
  because the planner could list skills but not read one. It holds `get_skill` (a read-only builder tool), # Changing
  rule 1 names it, and rule 26 says update_skill replaces the whole body: read it first, change only what was asked.

Neutral words, none from a gate brief (checked against CAP3's).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core import http_facts
from test_a_change_changes_an_http_tools_address import _read, _tool, _update  # noqa: F401 — fixtures
from test_a_change_makes_the_http_tools_it_uses import SHOP, _call, fresh  # noqa: F401 — fixtures

PLANNER = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" / "planner.json"
HINT = "keeps one call's value as written"


def _planner() -> dict:
    return json.loads(PLANNER.read_text("utf-8"))


def _changing_rule(n: int) -> str:
    changing = _planner()["system_prompt"].split("\n# Changing", 1)[1].split("\n# ", 1)[0]
    found = re.search(rf"\n{n}\. (.*)", changing)
    assert found, f"no rule {n} under # Changing"
    return found.group(1)


async def _submit(message: str, *ops: dict, run_id: str) -> tuple[dict, object]:
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.native_builder.door import check
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"},
                                         "user_message": check(message).text})
    await SharedState(run).checkpoint()
    out, _ = await _call("submit_change", {"ops": list(ops)}, run)
    return out, run


class TestOneCallsValue:
    @pytest.mark.parametrize("url, value", [
        (f"{SHOP}/v2/items/1042", "1042"),
        (f"{SHOP}/v2/items/1042/notes", "1042"),
        (f"{SHOP}/v2/items?since=2026-11-01", "2026-11-01"),
        (f"{SHOP}/v2/items/{'3f2b8c1e-' + '9d4a-4b7e-8a1c-0e5f6d7a8b9c'}", "3f2b8c1e-9d4a-4b7e-8a1c-0e5f6d7a8b9c"),
        (f"{SHOP}/v2/items?id=1042", "1042"),
    ])
    def test_an_id_a_date_or_a_number_in_the_path_or_query(self, url, value):
        assert http_facts.one_calls_value(url) == value

    @pytest.mark.parametrize("url", [f"{SHOP}/v2/items", f"{SHOP}/v2/items/{{{{ item_id }}}}",
                                     f"{SHOP}/v2/items?status=new", f"{SHOP}/api/v1/items/7"])
    def test_words_a_version_one_digit_and_a_marked_value_are_none(self, url):
        assert http_facts.one_calls_value(url) is None


class TestTheCheck:
    async def test_a_curls_literal_id_is_sent_back_once_then_taken(self, client):
        await _tool()
        curl = f"curl {SHOP}/v2/orders/1042"
        op = _update(**{"from": {"curl": 1}})
        first, run = await _submit(f"This call works: {curl}", op, run_id="builder_f1377_once")
        assert HINT in first.get("error", "") and f"(1042 in {SHOP}/v2/orders/1042)" in first["error"], first
        assert "the same for every call" in first["error"]
        again, _ = await _call("submit_change", {"ops": [op]}, run)
        assert again.get("finish") == "submit_change", "said once: an address that is the same for every call stays"

    async def test_the_planners_own_literal_url_from_a_curl_is_sent_back_too(self, client):
        await _tool()
        out, _ = await _submit(f"This call works: curl {SHOP}/v2/orders/1042",
                               _update(**{"from": {"curl": 1}, "url": f"{SHOP}/v2/orders/1042"}),
                               run_id="builder_f1377_literal")
        assert HINT in out.get("error", ""), out

    async def test_a_marked_value_is_taken_with_the_curls_method(self, client):
        await _tool()
        out, _ = await _submit(f"This call works: curl {SHOP}/v2/orders/1042",
                               _update(**{"from": {"curl": 1}, "url": f"{SHOP}/v2/orders/{{{{ order_id }}}}"}),
                               run_id="builder_f1377_marked")
        assert out.get("finish") == "submit_change", out
        assert out["ops"][0]["fields"]["url"] == f"{SHOP}/v2/orders/{{{{ order_id }}}}"

    async def test_an_address_with_no_such_value_is_taken_as_before(self, client):
        await _tool()
        out, _ = await _submit(f"This call works: curl {SHOP}/v2/orders?status=new", _update(**{"from": {"curl": 1}}),
                               run_id="builder_f1377_plain")
        assert out.get("finish") == "submit_change", out


class TestWhatThePlannerIsTold:
    def test_rule_23_makes_one_calls_value_an_input_unless_the_address_is_the_same_for_every_call(self):
        rule = _changing_rule(23)
        assert "from call to call" in rule and "belongs to one call" in rule and "becomes an input" in rule, rule
        assert "never kept as written" in rule and "the same for every call" in rule, rule

    def test_update_http_tools_url_says_the_same(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS

        [schema] = [t["function"]["parameters"] for t in BUILDER_TOOL_SCHEMAS
                    if t["function"]["name"] == "update_http_tool"]
        said = schema["properties"]["url"]["description"]
        assert "changes from call to call" in said and "never one call's value as written" in said, said


class TestASkillIsReadFirst:
    def test_the_planner_holds_get_skill_a_read_only_tool(self):
        from core.product_tools import build

        assert "get_skill" in _planner()["tools"] and "get_skill" in build.READS

    def test_rule_1_names_it(self):
        rule = _changing_rule(1)
        assert "`get_skill` a skill's whole text" in rule, rule

    def test_rule_26_reads_it_before_update_skill_and_changes_only_what_was_asked(self):
        rule = _changing_rule(26)
        assert "update_skill" in rule and "whole `body`" in rule and "get_skill first" in rule, rule
        assert "only what they asked changed" in rule and "the rest reads as it did" in rule, rule
