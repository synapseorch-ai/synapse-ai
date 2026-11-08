"""Questions a run or a conversation asks, answered from the CLI; schedules changed; a remote key kept
(CLI1, D294, F711 F712 F717).
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from _fakes import engine_events as E
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from cli_harness import answers, main
from synapse.client import errors, target

# ── F711: a team's several questions, each answered by its id ───────────────

LEAD, ANA, BEN = "lead_cli1", "ana_cli1", "ben_cli1"
DANGER = {"type": "function", "function": {"name": "danger", "description": "changes a thing",
                                           "parameters": {"type": "object", "properties": {"item": {"type": "string"}}}}}


@pytest.fixture
def team(monkeypatch):
    """A Team agent whose two members each stop on a gated call; what ran, in order (TEAM1e's own setup)."""
    import core.react_engine as re_mod

    llm = install(monkeypatch, RoutedLLM())
    ran: list[tuple[str, dict]] = []
    real = re_mod.run_agent_step

    async def executor(name, args):
        ran.append((name, dict(args)))
        return f"{name} done for {args.get('item')}"

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") in (ANA, BEN):
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    monkeypatch.setattr(re_mod, "run_agent_step", wrapped)
    llm.otherwise(["\n".join([tool_call("assign", member="Ana", task="CHANGE A"),
                              tool_call("assign", member="Ben", task="CHANGE B")]),
                   *[tool_call("wait", seconds=10 + i) for i in range(8)], "both changed"])
    llm.on("CHANGE A", [tool_call("danger", item="a"), "A changed"])
    llm.on("CHANGE B", [tool_call("danger", item="b"), "B changed"])
    return ran


async def _team_workflow():
    asks = {"tools": ["all"], "skip_default_tools": True, "tool_policy": {"undeclared": "confirm"}, "max_turns": 4}
    await S.seed_agents([
        S.make_agent(id=ANA, name="Ana", **asks), S.make_agent(id=BEN, name="Ben", **asks),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], skip_default_tools=True,
                     delegate_agent_ids=[ANA, BEN], team={"mode": "team"}),
    ])
    orch = S.make_orchestration(id="orch_cli1_team", name="Change both", entry_step_id="s1", steps=[
        {"id": "s1", "name": "Change", "type": "agent", "agent_id": LEAD, "prompt_template": "PLAN IT",
         "max_turns": 12, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    return orch


class TestATeamsQuestions:
    async def test_each_is_named_with_its_id_and_answered_by_it(self, synapse, team):
        """Before (F711): the CLI showed the step's one prompt and answered without a question_id, so only the
        first member's question could ever be answered from the CLI, and `runs show` named neither."""
        orch = await _team_workflow()
        code, _, err = await synapse("run", orch["id"])
        assert code == errors.WAITING
        assert "Waiting for 2 answers" in err
        assert "Ana" in err and "Ben" in err and err.count("--question") == 2
        run_id = err.split("synapse runs answer ")[1].split()[0]

        code, out, _ = await synapse("runs", "show", run_id, "--json")
        asked = {q["member_name"]: q["question_id"] for q in json.loads(out)["human_input_required"]["questions"]}
        code, out, _ = await synapse("runs", "show", run_id)
        assert code == errors.WAITING and asked["Ben"] in out and asked["Ana"] in out

        code, _, err = await synapse("runs", "answer", run_id, "--question", asked["Ben"], "--approve")
        assert code == errors.WAITING, err
        assert team == [("danger", {"item": "b"})], "only Ben's call ran"

        code, out, err = await synapse("runs", "answer", run_id, "--question", asked["Ana"], "--reject",
                                       "--reason", "not today")
        assert code == 0, err
        assert team == [("danger", {"item": "b"})], "Ana's was refused, never run"

    async def test_a_question_no_longer_asked_is_refused(self, synapse, team):
        orch = await _team_workflow()
        _, _, err = await synapse("run", orch["id"])
        run_id = err.split("synapse runs answer ")[1].split()[0]
        code, _, err = await synapse("runs", "answer", run_id, "--question", "q_not_asked", "--approve")
        assert code == errors.ERROR and "q_not_asked is not asked now" in err, err

    async def test_a_person_answers_each_at_the_terminal(self, synapse, team, monkeypatch):
        orch = await _team_workflow()
        answers(monkeypatch, "y", "n", "")
        code, out, err = await synapse("run", orch["id"])
        assert code == 0, err
        assert len(team) == 1


# ── F712: a conversation's held call, answered without a terminal ───────────

APPROVAL = {
    "type": "human_input_required", "kind": "tool_approval",
    "prompt": "The agent wants to run **send_email**. Allow it?",
    "tool_name": "send_email", "tool_args": {"to": "a@b.c"}, "category": "external",
    "approval_id": "appr-1", "fields": [],
}


class TestAHeldCall:
    async def test_a_script_approves_it_by_its_session(self, synapse, seed_agent, monkeypatch):
        """Before (F712): `chat -m` exited 3 with "Continue with: synapse chat --session …", which opened a REPL;
        a script had no way to answer it."""
        import core.react_engine as re
        await seed_agent()
        calls = []

        async def loop(*args, **kwargs):
            calls.append(kwargs.get("resume_state"))
            if len(calls) == 1:
                yield APPROVAL
            else:
                yield {"type": "final", "response": "Sent."}
        monkeypatch.setattr(re, "run_react_loop", loop)

        code, _, err = await synapse("chat", "-m", "email them")
        assert code == errors.WAITING
        assert "--approve|--reject" in err and "send_email" in err
        session = err.split("synapse chat --session ")[1].split()[0]

        code, out, err = await synapse("chat", "--session", session, "--approve")
        assert code == 0, err
        assert out.strip().endswith("Sent.")
        assert calls[1] is not None, "the answer resumed the turn from its snapshot"

    async def test_a_refusal_carries_its_reason(self, synapse, seed_agent, monkeypatch):
        import core.react_engine as re
        await seed_agent()
        seen = []

        async def loop(*args, **kwargs):
            seen.append(kwargs.get("resume_state"))
            if len(seen) == 1:
                yield APPROVAL
            else:
                yield {"type": "final", "response": "Not sent."}
        monkeypatch.setattr(re, "run_react_loop", loop)
        _, _, err = await synapse("chat", "-m", "email them")
        session = err.split("synapse chat --session ")[1].split()[0]
        code, out, _ = await synapse("chat", "--session", session, "--reject", "--reason", "wrong person")
        assert code == 0 and out.strip().endswith("Not sent.")

    async def test_an_answer_needs_its_session(self, synapse, seed_agent):
        await seed_agent()
        code, _, err = await synapse("chat", "--approve")
        assert code == errors.USAGE and "--session" in err

    async def test_nothing_held_is_said(self, synapse, seed_agent, monkeypatch):
        import core.react_engine as re
        await seed_agent()
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([{"type": "final", "response": "Hi."}]))
        code, _, err = await synapse("chat", "-m", "hi", "--verbose")
        assert code == 0, err
        code, _, err = await synapse("chat", "--session", "api_none", "--approve")
        assert code == errors.ERROR and "not waiting on an approval" in err, err


# ── F717: a schedule changed ────────────────────────────────────────────────


class TestScheduleUpdate:
    async def test_its_timing_prompt_and_name_change(self, synapse, seed_agent, schedule_manager):
        await seed_agent(name="Digest")
        _, out, _ = await synapse("schedules", "create", "--agent", "Digest", "--every", "2h", "--json")
        made = json.loads(out)

        code, out, err = await synapse("schedules", "update", made["id"], "--cron", "0 9 * * 1",
                                       "--prompt", "this week", "--name", "Monday digest", "--json")
        assert code == 0, err
        changed = json.loads(out)
        assert changed["schedule_type"] == "cron" and changed["cron_expression"] == "0 9 * * 1"
        assert changed["prompt"] == "this week" and changed["name"] == "Monday digest"

        code, out, _ = await synapse("schedules", "update", made["id"], "--every", "30m", "--json")
        assert (json.loads(out)["interval_value"], json.loads(out)["interval_unit"]) == (30, "minutes")

    async def test_nothing_to_change_is_a_usage_error(self, synapse, seed_agent, schedule_manager):
        await seed_agent(name="Digest")
        _, out, _ = await synapse("schedules", "create", "--agent", "Digest", "--every", "2h", "--json")
        code, _, err = await synapse("schedules", "update", json.loads(out)["id"])
        assert code == errors.USAGE and "--every" in err


# ── a remote Synapse's key, kept ────────────────────────────────────────────


class TestAKeptKey:
    async def test_kept_after_it_is_checked_then_used_then_forgotten(self, live_server, api_key, monkeypatch,
                                                                    capsys, seed_agent):
        import asyncio
        import io

        await seed_agent(name="Kept")
        # Not loopback to the CLI: as a remote Synapse is, so no key of its own is minted.
        url = live_server.url.replace("127.0.0.1", "localhost")
        monkeypatch.setattr(target, "_is_loopback", lambda host: False)
        monkeypatch.setattr("sys.stdin", io.StringIO(api_key["raw"] + "\n"))
        code, out, err = await asyncio.to_thread(main, monkeypatch, capsys, "login", "--url", url, "--api-key-stdin")
        assert code == 0, err
        assert stat.S_IMODE(os.stat(target.config_path()).st_mode) == 0o600

        code, out, err = await asyncio.to_thread(main, monkeypatch, capsys, "agents", "list", "--url", url)
        assert code == 0 and "Kept" in out, err

        code, out, _ = await asyncio.to_thread(main, monkeypatch, capsys, "target", "--url", url, "--json")
        info = json.loads(out)
        assert info["key_from"] == "kept" and info["reachable"] is True and api_key["raw"] not in out

        code, out, _ = await asyncio.to_thread(main, monkeypatch, capsys, "logout", "--url", url)
        assert "Forgot" in out
        code, _, err = await asyncio.to_thread(main, monkeypatch, capsys, "agents", "list", "--url", url)
        assert code == errors.REFUSED and "needs an API key" in err

    async def test_a_refused_key_is_never_kept(self, live_server, monkeypatch, capsys):
        import asyncio
        import io

        monkeypatch.setattr(target, "_is_loopback", lambda host: False)
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-syn-" + "0" * 32 + "\n"))
        code, _, _ = await asyncio.to_thread(main, monkeypatch, capsys, "login", "--url", live_server.url,
                                             "--api-key-stdin")
        assert code == errors.REFUSED
        assert "kept_key" not in (target.config_path().read_text() if target.config_path().exists() else "")
