"""A change edits a Python tool's code, tested with no network before the yes (F1325; Naveen 11-04, eb).

"The totals tool rounds wrong, fix it" had no op: a change could only hand a new Python tool to the maker, or put a
deleted one back. Now the planner reads the tool's code (`read_workspace` topic `tool`), and proposes
`update_python_tool`: passages quoted exactly as they stand, each with its new text (F1258's way), or a short tool's
whole code, and one to three sample inputs. Code applies the edits, and runs the edited code on each sample with **no
network** (Naveen: no internet before the yes) before anything is shown for approval. The review shows a diff of before
and after and what each sample printed. A test that fails goes back to the planner once, and then is said, with
nothing changed. Where the network can't be turned off, or the fix needs it to be tested, the card offers "Test this
fix", which the person presses after reading the diff; the yes waits for a pass. The same rules as making one hold:
no key in the code, no workspace variable named by the model, and the host's plan gate. Its undo puts the old code
back.

Every key below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import traceback

import pytest

from core.execution import ExecResult, ExecUnsupported, Network, register_runner
from core.native_builder.change_tools import KEY_REFUSAL
from test_a_change_makes_the_http_tools_it_uses import (  # noqa: F401 — fixtures
    _call, _change, _public_dns, _run, _tools, fresh)

OLD = ("import json\n"
       "rows = _args.get('rows') or []\n"
       "print(json.dumps({'total': round(sum(rows), 0)}))\n")
FIND, REPLACE = "round(sum(rows), 0)", "round(sum(rows), 2)"
SAMPLE = {"rows": [1.234, 2.0]}
KEY = "sk-live-" + "abcdefghijklmnopqrstuvwx" + "1234"


class Runner:
    """The sandbox, faked in-process: it runs the wrapped code, records what each run asked for, and makes a call
    out fail when asked for no network, as the real runner guarantees."""

    def __init__(self, *, no_network: bool = True):
        self.asked: list[Network] = []
        self.no_network = no_network

    async def run(self, request):
        self.asked.append(request.network)
        if request.network == Network.NONE and not self.no_network:
            raise ExecUnsupported("this host's sandbox can't run code with no network")
        out, err, code = io.StringIO(), "", 0
        real, resolve = socket.socket.connect, socket.getaddrinfo

        def refused(*a, **k):
            raise OSError(101, "Network is unreachable")

        if request.network == Network.NONE:  # no name resolved, no connection made: nothing leaves this test
            socket.socket.connect = refused
            socket.getaddrinfo = refused
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(request.code, "<tool>", "exec"), {"__name__": "__main__"})
        except Exception:  # noqa: BLE001 — what the code raised is its stderr
            err, code = traceback.format_exc(), 1
        finally:
            socket.socket.connect, socket.getaddrinfo = real, resolve
        return ExecResult(exit_code=code, stdout=out.getvalue(), stderr=err)


@pytest.fixture
def runner():
    held = Runner()
    register_runner(held)
    yield held
    register_runner(None)


async def _tool(code: str = OLD, **more) -> dict:
    from core.store.resources import save_tool

    tool = {"name": "region_totals", "description": "Totals a region's rows", "tool_type": "python", "code": code,
            "inputSchema": {"type": "object", "properties": {"rows": {"type": "array"}}}, "globals": ["REGION"],
            **more}
    await save_tool(tool)
    return tool


def _edit(find=FIND, replace=REPLACE, samples=(SAMPLE,), **fields) -> dict:
    return {"op": "update_python_tool", "target": {"kind": "tool", "name": "region_totals"},
            "fields": {"edits": [{"find": find, "replace": replace}], "test_args": list(samples), **fields}}


async def _submitted(ops: list[dict], run=None) -> dict:
    out, _ = await _call("submit_change", {"ops": ops}, run or await _run())
    return out


async def _check(ops: list[dict]):
    """The change checked, its run keeping the review as the route does (a card's press reads it there)."""
    from core.orchestration.state import SharedState

    run = await _run()
    checked, events = await _call("check_change", {"draft": {"finish": "submit_change", "ops": ops}}, run)
    run.shared_state["change_checked"] = json.dumps(checked)
    await SharedState(run).checkpoint()
    return run, checked, events


async def _apply(run):
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return await _call("apply_change", {}, run)


# ── tested with no network, shown as a diff, made on the yes ────────────────────────────────────────────────────────

async def test_an_edit_is_tested_with_no_network_and_shown_as_a_diff_before_the_yes(client, runner):
    await _tool()
    out = await _submitted([_edit()])
    assert out.get("finish") == "submit_change", out
    assert runner.asked == [Network.NONE], "the test before the yes runs with no network"
    _, checked, done, _ = await _change({"ops": out["ops"]})
    review = checked["review"]
    assert review.startswith('- Change the tool "region_totals" — its code'), review
    assert f"-print(json.dumps({{'total': {FIND}}}))" in review and f"+print(json.dumps({{'total': {REPLACE}}}))" in review
    assert 'tested with {"rows": [1.234, 2.0]}: printed {"total": 3.23}' in review, review
    assert done["applied"] == ["update_python_tool"], done


async def test_after_the_yes_its_code_is_the_edited_code_and_the_variables_it_reads_stay(client, runner):
    await _tool()
    _, _, done, _ = await _change({"ops": (await _submitted([_edit()]))["ops"]})
    tool = (await _tools())["region_totals"]
    assert tool["code"] == OLD.replace(FIND, REPLACE) and tool["globals"] == ["REGION"], tool


async def test_its_undo_puts_the_old_code_back(client, runner):
    await _tool()
    first, _, done, _ = await _change({"ops": (await _submitted([_edit()]))["ops"]})
    assert done["applied"] == ["update_python_tool"], done
    _, checked, undone, _ = await _change({"undo_of": first.run_id})
    assert undone and undone["applied"] == ["update_python_tool"], (checked, undone)
    assert (await _tools())["region_totals"]["code"] == OLD


# ── what goes back to the planner, and what is said ─────────────────────────────────────────────────────────────────

async def test_a_failing_test_goes_back_to_the_planner_once_then_is_said(client, runner):
    await _tool()
    run = await _run()
    broken = _edit(replace="round(sum(rows) / 0, 2)")
    first = await _submitted([broken], run)
    assert "fails its test" in first.get("error", "") and "ZeroDivisionError" in first["error"], first
    second = await _submitted([broken], run)
    assert second.get("finish") == "submit_change", "asked once to fix it: the second is kept, to be said"
    _, checked, done, _ = await _change({"ops": second["ops"]})
    assert checked["outcome"] == "said" and done is None, checked
    assert 'fails its test with {"rows": [1.234, 2.0]}' in checked["say"] and "Nothing was changed" in checked["say"]
    assert (await _tools())["region_totals"]["code"] == OLD


async def test_a_passage_that_is_not_in_its_code_is_sent_back(client, runner):
    await _tool()
    out = await _submitted([_edit(find="round(sum(rows), 1)")])
    assert "round(sum(rows), 1)" in out.get("error", "") and "as it stands" in out["error"], out


# ── where the network is needed, or can't be off ────────────────────────────────────────────────────────────────────

NETWORKED = OLD.replace("print(", "import urllib.request\nurllib.request.urlopen('https://api.example.com/x')\nprint(")


async def _test_card(events) -> dict:
    [card] = [e for e in events if e.get("type") == "action_needed" and "Test this fix" in json.dumps(e)]
    return card


async def test_a_call_out_fails_with_no_network_and_its_card_offers_test_this_fix(client, runner):
    await _tool(code=NETWORKED)
    out = await _submitted([_edit()])
    assert out.get("finish") == "submit_change", out
    assert runner.asked == [Network.NONE], "it was tried with no network, where its call out failed"
    _, checked, events = await _check(out["ops"])
    assert "needs the internet to test" in checked["review"], checked["review"]
    await _test_card(events)


async def test_where_the_network_cant_be_off_the_yes_waits_for_test_this_fix_to_pass(client):
    held = Runner(no_network=False)
    register_runner(held)
    try:
        await _tool()
        out = await _submitted([_edit()])
        assert out.get("finish") == "submit_change", out
        run, checked, events = await _check(out["ops"])
        done, _ = await _apply(run)
        assert done["applied"] == [] and "Test this fix" in " ".join(done["failed"]), done
        assert (await _tools())["region_totals"]["code"] == OLD, "never made before its test passed"
        button = (await _test_card(events))["buttons"][0]
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.roles import Role

        areas.ensure_loaded()
        tested = await registry.invoke(button["tool"], Caller(role=Role.OWNER, via="assistant", user_id="u1"),
                                       {**button["arguments"], "confirm_token": button["token"]})
        assert "3.23" in json.dumps(tested) and held.asked[-1] == Network.INTERNET, tested
        _, _, again, _ = await _change({"ops": out["ops"]})
        assert again["applied"] == ["update_python_tool"], again
    finally:
        register_runner(None)


# ── the rules that make a Python tool ───────────────────────────────────────────────────────────────────────────────

async def test_a_key_in_the_new_code_is_never_kept(client, runner):
    await _tool()
    _, checked, done, _ = await _change({"ops": [_edit(replace=f"round(sum(rows), 2)  # {KEY}")]})
    assert checked["outcome"] == "said" and KEY_REFUSAL in checked["say"] and KEY not in json.dumps(checked)


async def test_it_names_no_variable_of_its_own(client, runner):
    await _tool()
    out = await _submitted([_edit(globals=["SECRET_KEY"])])
    assert "cannot set globals" in out.get("error", ""), out


async def test_the_hosts_plan_gate_refuses_it_in_its_words(client, runner):
    from core import python_tool

    async def gate(tool):
        return "Custom Python tools are not in this workspace's plan."

    await _tool()
    python_tool.set_check(gate)
    try:
        _, checked, done, _ = await _change({"ops": (await _submitted([_edit()]))["ops"]})
    finally:
        python_tool.set_check(None)
    assert "not in this workspace's plan" in checked.get("say", "") and done is None, checked


@pytest.mark.parametrize("name, words", [("no_such_tool", "No Python tool"), ("fetch_orders", "not a Python tool")])
async def test_a_tool_that_is_not_there_or_not_python_is_refused(client, runner, name, words):
    from core.store.resources import save_tool

    await save_tool({"name": "fetch_orders", "description": "x", "tool_type": "http", "method": "GET",
                     "url": "https://shop.tidewell.example/orders", "headers": {}})
    _, checked, _, _ = await _change({"ops": [{**_edit(), "target": {"kind": "tool", "name": name}}]})
    assert words in checked.get("say", ""), checked


# ── what the planner reads and is told ──────────────────────────────────────────────────────────────────────────────

async def test_the_planner_reads_its_code(client):
    await _tool()
    out, _ = await _call("read_workspace", {"topic": "tool", "name": "region_totals"}, await _run())
    assert out["code"] == OLD and out["reads_variables"] == ["REGION"] and out["kind"] == "python", out


def test_the_planner_is_told_how_a_python_tool_is_fixed():
    from pathlib import Path

    from core.native_builder.change_tools import SUBMIT

    assert "update_python_tool {edits: [{find, replace}]" in SUBMIT["function"]["description"]
    rules = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" /
                        "planner.json").read_text())["system_prompt"]
    changing = rules.split("\n# Changing", 1)[1].split("\n# ", 1)[0]
    rule = next(line for line in changing.splitlines() if "update_python_tool" in line)
    for word in ("read_workspace", "find", "test_args", "needs_network"):
        assert word in rule, (word, rule)


# ── over MCP (Naveen 11-04) ─────────────────────────────────────────────────────────────────────────────────────────

async def test_update_python_tool_is_served_to_mcp_clients_its_new_code_in_the_preview(client, runner):
    from core.mcp_server import catalog
    from core.product_tools import areas, build, confirm, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role
    from test_a_change_makes_the_http_tools_it_uses import _Server

    [served] = [t for t in catalog.builder_tools() if t.name == "update_python_tool"]
    assert served.annotations.destructiveHint and not served.annotations.readOnlyHint
    await _tool()
    new = OLD.replace(FIND, REPLACE)
    areas.ensure_loaded()
    caller, args = Caller(role=Role.OWNER, via="mcp", user_id="u1"), {"name": "region_totals", "code": new}
    with build.using_module(_Server()):
        preview = await registry.invoke("update_python_tool", caller, args)
        assert preview["status"] == "needs_confirmation", "a client confirms what it changes"
        shown = preview["preview"]
        assert shown["will_change"]["code"] == new and f"+print(json.dumps({{'total': {REPLACE}}}))" in shown["diff"]
        done = await registry.invoke("update_python_tool", caller, {**args, confirm.TOKEN_ARG: preview["confirm_token"]})
    assert "updated" in json.dumps(done) and (await _tools())["region_totals"]["code"] == new


async def test_test_code_change_is_the_apps_own_never_an_mcp_clients(client):
    from core.mcp_server import catalog
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.errors import ToolRefusal
    from core.product_tools.roles import Role

    assert "test_code_change" not in catalog.SERVED_BUILDER_TOOLS
    assert "test_code_change" not in {t.name for t in catalog.builder_tools()}
    areas.ensure_loaded()
    with pytest.raises(ToolRefusal):
        await registry.invoke("test_code_change", Caller(role=Role.OWNER, via="mcp", user_id="u1"),
                              {"run_id": "builder_x", "op": 0})
