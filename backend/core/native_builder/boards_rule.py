"""When a plan uses a board, and what its agents are told about it (F1254; Naveen's picks, 10-30, D339's last section).

None of Naveen's nine runs used a board, and the post generator kept nothing between runs, so it repeated itself. A
board is for work a run cannot hand on any other way: work found mid-run that other steps claim, work that outlives
the run (a later run or a person takes it up), and items tracked over time ("already posted"). A straight hand-off
from one step to the next is the step's output, and a known list is a Loop or a Parallel.

* `GUIDANCE`: what the planner's pack says (its rule 4 says to follow the pack).
* `issues`: the build check, from the plan's own parts, never its words. A board its agents write that nothing reads,
  and a step's results handed to the next through a board, are sent back with what to do instead.
* `with_board_lines`: each new agent with board tools gets a section code writes from those tools, after the
  instructions the writer wrote; it is on the card the person approves, and it is what is saved.
* `settle` and `give_line` (F1736): a plan that names several boards keeps them. Each one already here is the
  person's to give (`yours`): the build makes at most one board itself and gives the rest nothing, and the card says
  which boards to open on the Boards screen and what to add there.
"""
from __future__ import annotations

import re

from core.native_builder import objects as O

WRITES = ("board_add", "board_update")
READS = ("board_list", "board_read", "board_claim")

GUIDANCE = (
    "Hand a step's results to the next step through its output ({{ state.<step id> }}), and go through a known list "
    "with a Loop or a Parallel, never through a board. A board is only for: work found mid-run that other steps "
    "claim (board_claim); work that outlives the run, which a later run or a person takes up (name it in boards); "
    "and items tracked over time. A scheduled workflow whose runs must not repeat what an earlier run did keeps a "
    "board of what it did (\"Posted\"), reads it first and adds to it.")


def _board_tools(plan: O.PlanDraft) -> dict[str, set[str]]:
    from core.boards import TOOL_NAMES

    return {a.name: {t for t in a.tools if t in TOOL_NAMES} for a in plan.agents}


def _next_ids(s: O.Step) -> list[str]:
    return [x for x in [s.next, *((s.branches or {}).values())] if x and x != "end"]


def _after(start: str, by_id: dict[str, O.Step]) -> set[str]:
    """The steps a run reaches from `start`, along next and branches."""
    seen: set[str] = set()
    todo = list(_next_ids(by_id[start])) if start in by_id else []
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        todo += _next_ids(by_id[cur])
    return seen


def issues(plan: O.PlanDraft) -> list[str]:
    """A board nobody reads, and a hand-off through a board, in this plan's new agents' steps."""
    tools = _board_tools(plan)
    steps = [s for s in plan.steps if s.type == "agent" and tools.get(s.agent or "")]
    if not steps:
        return []
    writers = [s for s in steps if tools[s.agent] & set(WRITES)]
    readers = [s for s in steps if tools[s.agent] & set(READS)]
    named = bool(plan.boards)
    found: list[str] = []
    if writers and not readers and not named:
        who = ", ".join(sorted({s.id for s in writers}))
        found.append(f"boards: step {who} adds to a board that nothing reads. Pass its results on as the step's "
                     f"output ({{{{ state.{writers[0].id} }}}}) and take the board tools out, or name the board in "
                     "boards if the person keeps it. " + GUIDANCE)
    if named or plan.schedule:
        return found  # kept past the run: a person reads it, or a later run does
    by_id = {s.id: s for s in plan.steps}
    in_bodies = {b for s in plan.steps if s.type in ("loop", "parallel") for b in (s.body or [])}
    for w in writers:
        for r in readers:
            if r.id == w.id or r.id in in_bodies or w.id in in_bodies or "board_claim" in tools[r.agent]:
                continue
            if r.id in _after(w.id, by_id):
                found.append(f"step {w.id} hands its results to step {r.id} through the board: pass them on as "
                             f"{w.id}'s output, which {r.id}'s prompt reads as {{{{ state.{w.id} }}}}, and take the "
                             "board tools out. " + GUIDANCE)
    return found


def board_lines(agent_tools: set[str], board: str) -> str:
    """The section a new agent with board tools is given, from its tools alone."""
    lines = [f"## Your board\nYou work {board}."]
    if agent_tools & {"board_list", "board_read"}:
        lines.append("Read it first (board_list, then board_read for a card), so nothing already there is done "
                     "again.")
    if "board_claim" in agent_tools:
        lines.append("Claim a card before you work on it (board_claim), so no one else takes it too.")
    if "board_add" in agent_tools:
        lines.append("Add a card for each thing you do (board_add).")
    if "board_update" in agent_tools:
        lines.append("Mark a card done when it is (board_update), with what came of it.")
    return " ".join(lines[:1]) + "\n" + " ".join(lines[1:]) if len(lines) > 1 else lines[0]


def with_board_lines(plan: O.PlanDraft, prompts: dict[str, str]) -> dict[str, str]:
    """Each new agent's instructions, with its board section after them when it has board tools."""
    tools = _board_tools(plan)
    names = [b.name for b in plan.boards]
    board = (f'the shared boards {listed(names)}; each card goes on one of them, naming the board it belongs on'
             if len(names) > 1 else f'the board "{names[0]}"' if names else "this workflow's own board")
    return {name: (f"{text.rstrip()}\n\n{board_lines(tools[name], board)}" if tools.get(name) else text)
            for name, text in prompts.items()}


# ── several boards named (F1736) ────────────────────────────────────────────────────────────────────────────────────
#: The Boards screen's words for a board's access list and its first access (frontend/src/components/boards:
#: AccessList.tsx's heading, board.ts's ACCESS_LABEL), so the card names what the person sees there.
ACCESS_LIST = "Who can use this board"
ACCESS_WORDS = {"add": "Add cards", "work": "Read, claim and update"}


def listed(names: list[str]) -> str:
    """'"A"', '"A" and "B"', '"A", "B" and "C"'."""
    quoted = [f'"{n}"' for n in names]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _named_as_boards(text: str, shared: list[str]) -> list[str]:
    """The shared boards the words name as boards: '"Sales" board', 'Sales board', 'board "Sales"'. A name used as a
    word alone ("the sales follow-up", "a support reply") names no board."""
    quote = "[\"'\u201c\u201d\u2018\u2019]"
    found = []
    for name in shared:
        n = re.escape(name)
        if m := re.search(rf"(?<!\w){quote}?{n}{quote}?\s+board\b|\bboard\s+{quote}{n}{quote}", text, re.I):
            found.append((m.start(), name))
    return [name for _, name in sorted(found)]  # in the order the words name them


def settle(plan: O.PlanDraft, shared: list[str]) -> None:
    """Each board the plan names, as it is here: `existing` with this workspace's name for it, and, where it names
    more than one, each one already here `yours`. A plan with no board that names several shared boards as boards in
    its words names those. Code alone decides both, every time: whatever the planner wrote for them is replaced."""
    here = {b.lower(): b for b in shared}
    if not plan.boards and here:
        text = " ".join([plan.title, plan.summary, *(f"{s.name} {s.does} {s.prompt or ''}" for s in plan.steps)])
        if len(named := _named_as_boards(text, list(here.values()))) > 1:
            plan.boards = [O.BoardPart(name=n) for n in named]
    for b in plan.boards:
        b.existing = b.name.lower() in here
        b.name = here.get(b.name.lower(), b.name)
        b.yours = b.existing and len(plan.boards) > 1


def give_line(plan: O.PlanDraft) -> list[str]:
    """What the card says about the boards the person gives access to: which, where, and with what access."""
    yours = [b.name for b in plan.boards if b.yours]
    if not yours:
        return []
    tools = set().union(*_board_tools(plan).values()) if plan.agents else set()
    access = ACCESS_WORDS["work"] if tools & {"board_list", "board_read", "board_claim", "board_update"} else (
        ACCESS_WORDS["add"])
    one = len(yours) == 1
    return [f"It works the shared board{'' if one else 's'} {listed(yours)}, which {'is' if one else 'are'} already "
            f"here. Give it access{'' if one else ' to each'} on the Boards screen: open the board and, under "
            f"\"{ACCESS_LIST}\", add \"{plan.title}\" with \"{access}\". Until then, its agents can't post there."]
