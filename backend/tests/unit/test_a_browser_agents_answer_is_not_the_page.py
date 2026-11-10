"""A Browser agent's answer is its own words, never the page it read (F1510, eb's QA B18; Naveen's pick 11-10).

On prod (run_01a1be82…, cloud-v2.0.0) a Browser agent on Nova Lite read the-internet's /checkboxes page and gave as
its answer "### CURRENT BROWSER STATE" and the page's snapshot yaml, so the run's Result was a page dump rather than
"Both checkboxes are ticked." The engine puts the page's snapshot in the turn's tail for the agent to read, and nothing
checked that the answer wasn't that page. BRW4's replay (11-10) of the engine's own last turn reproduced it once in 20
on Nova Lite as a workflow step after a Replay step, never as a lone agent (Nova Lite and Nova Pro, 20 tries), and
never on gpt-oss-120b or Llama 3.3 70B (5 each).

A final reply that is the page (most of its lines are the snapshot's own structure: its refs, its yaml, the engine's
markers, with no answer line before them) is asked for once more, in the agent's own words, as an all-reasoning reply
is (F305). A second one fails the turn: "The model repeated the page instead of answering." An answer that quotes a
few of the page's lines (Nova Pro did, 3 of 10), or puts its own words in a yaml block, is an answer. A reply that names refs is only questioned in a turn
that read a page.
"""
from __future__ import annotations

import types

import pytest
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.llm_providers import EmptyAnswer

PAGE = """### Page
- Page URL: https://the-internet.herokuapp.com/checkboxes
- Page Title: The Internet
### Snapshot
```yaml
- generic [ref=e2]:
  - generic [ref=e6]:
    - heading "Checkboxes" [level=3] [ref=e7]
    - generic [ref=e8]:
      - checkbox [checked] [ref=e9]
      - text: checkbox 1
      - checkbox [checked] [ref=e10]
      - text: checkbox 2
```"""
#: What prod's Result showed: the engine's own heading and the page.
DUMP = "### CURRENT BROWSER STATE\n" + PAGE.split("### Snapshot\n", 1)[1]
#: The dump as Nova Lite gave it in BRW4's replay as a workflow step (1 of 20): the heading, the page's lines without
#: their "- ", and the snapshot without its fence.
REPRODUCED = """### CURRENT BROWSER STATE
Page URL: https://the-internet.herokuapp.com/checkboxes
Page Title: The Internet
Snapshot:
- generic [ref=e2]:
  - generic [ref=e6]:
    - heading "Checkboxes" [level=3] [ref=e7]
    - generic [ref=e8]:
      - checkbox [checked] [ref=e9]
      - text: checkbox 1
      - checkbox [checked] [ref=e10]
      - text: checkbox 2"""
#: Nova Pro's answers in the replay: an answer, then some of the page.
QUOTED = ("Both checkboxes on the page are ticked.\n\nHere's the relevant part of the page snapshot:\n```\n"
          "- generic [ref=e8]:\n  - checkbox [checked] [ref=e9]\n  - text: checkbox 1\n"
          "  - checkbox [checked] [ref=e10]\n  - text: checkbox 2\n```")
ANSWER = "Both checkboxes are ticked."
#: Nova Lite's other reply in the same replay: its own words, in a yaml block.
OWN_YAML = "```yaml\nCheckbox 1: checked\nCheckbox 2: checked\n```"
NUDGE = "your last reply repeated the page"


class _Browser:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.calls.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=PAGE)], isError=False)


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


async def _run(fake_llm, script, *, read_a_page=True):
    from core.react_engine import run_agent_step

    names = ("browser_navigate", "browser_snapshot")
    server = types.SimpleNamespace(agent_sessions={"browser_srv": _Browser()}, memory_store=None,
                                   tool_router={n: ("browser_srv", n) for n in names})
    fake_llm.script(([tool_call("browser_snapshot")] if read_a_page else []) + script)
    agent = S.make_agent(name="Checkbox Reader", type="browser", tools=["all"], skip_default_tools=True,
                         max_turns=8)
    return [ev async for ev in run_agent_step(
        message="Report whether both checkboxes on the page are ticked.", agent_id=agent["id"],
        session_id="s1", server_module=server, agent_override=agent, max_turns=8,
        tools_override=[_schema(n) for n in names])]


def _final(events) -> str:
    return next(ev["response"] for ev in events if ev.get("type") == "final")


def _asked_again(fake_llm) -> bool:
    return any(NUDGE in "".join(call.get("prompt_blocks") or [call.get("prompt_msg") or ""])
               for call in fake_llm.calls)


class TestThePageIsNotAnAnswer:
    @pytest.mark.parametrize("page", [DUMP, REPRODUCED, PAGE], ids=["prod", "replayed", "the-snapshot"])
    async def test_a_reply_that_is_the_page_is_asked_for_again_in_the_agents_words(self, fake_llm, page):
        events = await _run(fake_llm, [page, ANSWER])
        assert _final(events) == ANSWER
        assert len(fake_llm.calls) == 3
        assert _asked_again(fake_llm)

    async def test_a_second_one_fails_the_turn_with_why(self, fake_llm):
        with pytest.raises(EmptyAnswer, match="The model repeated the page instead of answering."):
            await _run(fake_llm, [DUMP, DUMP])
        assert len(fake_llm.calls) == 3


class TestAnAnswerIsAnAnswer:
    @pytest.mark.parametrize("said", [ANSWER, QUOTED, OWN_YAML, "Checkbox 1 and checkbox 2 are both ticked."])
    async def test_an_answer_even_one_quoting_the_page_is_taken_at_once(self, fake_llm, said):
        events = await _run(fake_llm, [said])
        assert _final(events) == said
        assert len(fake_llm.calls) == 2
        assert not _asked_again(fake_llm)

    async def test_a_turn_that_read_no_page_is_never_questioned(self, fake_llm):
        events = await _run(fake_llm, [DUMP], read_a_page=False)
        assert _final(events) == DUMP
        assert len(fake_llm.calls) == 1
