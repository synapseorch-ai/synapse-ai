"""
A tool call may be allowed, held for a person, or refused — on what the tool
does, not on its name.

The allowlist asks whether an agent may *hold* a tool. This asks whether this
call may *run*. Before it, the only write-vs-read distinction in the engine
was a sentence in the system prompt asking the model to check with the user,
and a builder gate that asked the model to confirm to itself.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tool_policy import DEFAULT_POLICY, decide, resolve_policy
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


def _tool(name="t", trusted=True, **hints):
    return VirtualTool(name, "d", {}, _ann(**hints) if hints else None, trusted=trusted)


READ_ONLY = dict(readOnlyHint=True, idempotentHint=True)
DESTRUCTIVE = dict(readOnlyHint=False, destructiveHint=True)
OPEN_WORLD = dict(readOnlyHint=True, idempotentHint=True, openWorldHint=True)


class TestTheDefaultChangesNothing:
    def test_every_category_defaults_to_allow(self):
        """A closed default would silently break every agent already running —
        the same reasoning as the DB allowlist's empty default."""
        assert set(DEFAULT_POLICY.values()) == {"allow"}
        policy = resolve_policy(agent=None, settings=None)
        for tool in (_tool(**DESTRUCTIVE), _tool(**OPEN_WORLD), _tool()):
            assert decide(tool, policy)[0] == "allow"


class TestResolvingThePolicy:
    def test_the_agent_overrides_the_deployment(self):
        policy = resolve_policy(
            agent={"tool_policy": {"destructive": "deny"}},
            settings={"tool_policy_defaults": {"destructive": "confirm", "open_world": "confirm"}},
        )
        assert policy["destructive"] == "deny"      # the agent's word
        assert policy["open_world"] == "confirm"    # the deployment's, untouched
        assert policy["undeclared"] == "allow"      # nobody spoke

    def test_nonsense_is_ignored_not_obeyed(self):
        policy = resolve_policy(
            agent={"tool_policy": {"destructive": "maybe", "invented": "deny"}},
            settings={"tool_policy_defaults": "not a dict"},
        )
        assert policy == DEFAULT_POLICY


class TestWhatDecidesWhat:
    @pytest.mark.parametrize("hints,category", [
        (DESTRUCTIVE, "destructive"),
        (OPEN_WORLD, "open_world"),
        ({}, "undeclared"),
        (READ_ONLY, "read_only"),
    ])
    def test_the_category_is_the_reason(self, hints, category):
        strict = {"destructive": "deny", "open_world": "confirm", "undeclared": "confirm"}
        assert decide(_tool(**hints), strict)[1] == category

    def test_destructive_outranks_open_world(self):
        """A tool that is both is judged on the worse of the two."""
        both = _tool(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
        strict = {"destructive": "deny", "open_world": "allow", "undeclared": "allow"}
        assert decide(both, strict) == ("deny", "destructive")

    def test_a_trusted_read_only_tool_always_passes(self):
        strict = {c: "deny" for c in DEFAULT_POLICY}
        assert decide(_tool(**READ_ONLY), strict) == ("allow", "read_only")


class TestTrustIsAsymmetric:
    """An external server's hints are a claim, not a boundary. Believed when
    they restrict; not when they reassure."""

    def test_a_stranger_claiming_to_be_safe_is_treated_as_unknown(self):
        strict = {"destructive": "allow", "open_world": "allow", "undeclared": "confirm"}
        assert decide(_tool(trusted=False, **READ_ONLY), strict) == ("confirm", "undeclared")

    def test_a_stranger_claiming_to_be_destructive_is_believed(self):
        strict = {"destructive": "deny", "open_world": "allow", "undeclared": "allow"}
        assert decide(_tool(trusted=False, **DESTRUCTIVE), strict) == ("deny", "destructive")

    def test_our_own_read_only_tool_passes(self):
        strict = {"destructive": "allow", "open_world": "allow", "undeclared": "confirm"}
        assert decide(_tool(trusted=True, **READ_ONLY), strict) == ("allow", "read_only")


# ── The gate in the loop ────────────────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "d",
                                             "parameters": {"type": "object", "properties": {}}}}


async def _run_agent(fake_llm, script, agent, executed=None, **kw):
    from core.react_engine import run_agent_step

    async def executor(name, args):
        if executed is not None:
            executed.append((name, args))
        return f"ran-{name}"

    fake_llm.script(script)
    kw.setdefault("tools_override", [_schema("danger")])
    return [ev async for ev in run_agent_step(
        message="go", agent_id=agent.get("id"), session_id="s1", server_module=_server(),
        agent_override=agent, tool_executor=executor, max_turns=5, **kw,
    )]


def _agent(policy=None):
    a = S.make_agent(tools=["all"], skip_default_tools=True)
    if policy:
        a["tool_policy"] = policy
    return a


class TestTheGateInTheLoop:
    async def test_allow_runs_the_call_exactly_as_before(self, fake_llm):
        executed = []
        events = await _run_agent(fake_llm, [tool_call("danger"), "done"], _agent(), executed)
        assert executed == [("danger", {})]
        assert not any(e["type"] == "human_input_required" for e in events)

    async def test_deny_refuses_and_tells_the_model_why(self, fake_llm):
        executed = []
        events = await _run_agent(fake_llm, [tool_call("danger"), "done"],
                                  _agent({"undeclared": "deny"}), executed)
        assert executed == []                       # never ran
        blocked = [e for e in events if e.get("preview", "").startswith("Blocked by policy")]
        assert blocked and "undeclared" in blocked[0]["preview"]
        # The model is told, so it can choose another route rather than retry.
        assert "blocked by this agent's tool policy" in fake_llm.calls[-1]["prompt_msg"]

    async def test_confirm_pauses_before_running_anything(self, fake_llm):
        executed = []
        events = await _run_agent(fake_llm, [tool_call("danger"), "done"],
                                  _agent({"undeclared": "confirm"}), executed)
        assert executed == []                       # the whole point
        pause = events[-1]
        assert pause["type"] == "human_input_required"
        assert pause["kind"] == "tool_approval"
        assert pause["tool_name"] == "danger"
        assert pause["approval_id"]
        assert [f["name"] for f in pause["fields"]] == ["approve", "reason"]
        assert fake_llm.call_count == 1             # the step stopped, it did not loop

    async def test_an_approval_carried_across_the_pause_runs_the_call_once(self, fake_llm):
        from core.react_engine import _approval_id
        executed = []
        approval = _approval_id("danger", {})
        events = await _run_agent(
            fake_llm, [tool_call("danger"), "done"], _agent({"undeclared": "confirm"}), executed,
            resume_state={"context_text": "User Request: go\n", "turn": 0,
                          "approved_calls": [approval]},
        )
        assert executed == [("danger", {})]
        assert not any(e["type"] == "human_input_required" for e in events)

    async def test_a_refusal_becomes_something_the_model_reads(self, fake_llm):
        from core.react_engine import _approval_id
        executed = []
        approval = _approval_id("danger", {})
        await _run_agent(
            fake_llm, [tool_call("danger"), "done"], _agent({"undeclared": "confirm"}), executed,
            resume_state={"context_text": "User Request: go\n", "turn": 0,
                          "denied_calls": [approval], "denied_reasons": {approval: "not on prod"}},
        )
        assert executed == []
        prompt = fake_llm.calls[-1]["prompt_msg"]
        assert "refused by the user" in prompt and "not on prod" in prompt

    async def test_an_approval_is_for_one_set_of_arguments(self, fake_llm):
        """Approving `ls` must not also approve `rm`."""
        from core.react_engine import _approval_id
        executed = []
        events = await _run_agent(
            fake_llm, [tool_call("danger", cmd="rm"), "done"], _agent({"undeclared": "confirm"}), executed,
            resume_state={"context_text": "User Request: go\n", "turn": 0,
                          "approved_calls": [_approval_id("danger", {"cmd": "ls"})]},
        )
        assert executed == []
        assert events[-1]["type"] == "human_input_required"

    async def test_a_read_only_tool_is_never_gated(self, fake_llm, monkeypatch):
        import core.react_engine as re_mod
        executed = []
        real = re_mod.aggregate_all_tools

        # The declaration the loop looks up carries the read-only hint.
        async def aggregated(*a, **k):
            all_tools, schema_map, ollama, tools_json = await real(*a, **k)
            return ([_tool("danger", **READ_ONLY)], schema_map, ollama, tools_json)

        monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
        events = await _run_agent(fake_llm, [tool_call("danger"), "done"],
                                  _agent({"undeclared": "deny", "destructive": "deny"}),
                                  executed, tools_override=None)
        assert executed == [("danger", {})]
        assert not any(e["type"] == "human_input_required" for e in events)
