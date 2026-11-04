"""The structure a plan should have, where code can tell (D340 D3, BLD1; TRI1's spec §4).

Judgement only where it is needed: a fetch is a Tool step, data goes through `{{ state.x }}`, and an agent is kept
for what needs a model. Most of that is the planner's to weigh (its prompt's "# Structure": helpers, a team, a Browser
agent, a transform for reshaping, one known call as a Tool step). Code holds only what it can tell from the plan alone,
never from its words:

* **A fetch is a Tool step**: a new agent the plan makes whose one tool reads a page or an API (a built-in page read,
  or a read-only GET HTTP tool here) does nothing a model is needed for; the read is a Tool step whose output the next
  step reads. An agent already here is the person's pick, and left alone (TRI1).
* **Agents asked for as a team are a team** (F1284, prod-06): two or more new agents, no team holding them, one's step
  reading another's through `{{ state.x }}`, in a build the quick classify itself gave the reason `team` (its closed
  list, `request.why`, as the effort offer reads it): one agent of type team, its lead, with them as its members. A
  pipeline of agents not asked for as a team is a plan like any other.
* **Code's work is a Transform** (F1291, CAP3's cap-04): a new agent holding `execute_python`, where code steps run,
  does with a model what a `transform` step (or a Python tool) does exactly. Where code steps do not run, it is the
  only way, and left alone.
* **An agent that works a named site is a browser agent** (F1301, cap-05): a new agent of type agent holding the in-app
  browser's tools, on steps whose words name an http(s) site, has no sites, so its first keystroke there is the
  person's turn; a browser agent with that site in `browser.sites` types there without one (read_only to look,
  co_drive to type, press or send).

A finding is sent back once, then said on the card: never a reason a plan cannot be built.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from core.native_builder import objects as O
from core.templating import STATE_REF, one_form

if TYPE_CHECKING:
    from core.native_builder.build_plan import Workspace

#: The sandbox's code tool an agent runs Python with: a new agent holding it, where code steps run, is a Transform's
#: work done by a model (F1291).
CODE_TOOL = "execute_python"
#: The built-in page reads a workflow's agent can call (the scraper's, `build_plan._BUILT_IN_SCRAPE`).
PAGE_READS = ("scrape_url", "scrape_structured", "search_page", "extract_links")


def _reads(name: str, custom: dict[str, dict]) -> bool:
    """Whether `name` only reads: a built-in page read, or an HTTP tool here that GETs and declares it only reads."""
    if name in PAGE_READS:
        return True
    tool = custom.get(name)
    if not tool or (tool.get("tool_type") or "http") != "http" or str(tool.get("method") or "").upper() != "GET":
        return False
    hints = tool.get("annotations") or {}
    return bool(hints.get("readOnlyHint") or tool.get("read_only"))


async def issues(plan: O.Plan, ws: Workspace, asked: dict | None = None) -> list[str]:
    """What the plan's structure should be instead, where code can tell; [] when it holds. `asked` is the quick
    classify's request, as the run holds it."""
    from core.store.resources import load_tools

    if not plan.agents:
        return []
    custom = {t.get("name"): t for t in await load_tools()}
    out = []
    steps = [s for s in (*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)) if s.type != "end"]
    for a in plan.agents:
        # Code's work is a Transform (F1291, cap-04): a new agent computing with `execute_python`, where code steps run.
        if ws.code_steps and any(str(t).split("__")[-1] == CODE_TOOL for t in a.tools):
            out.append(f"structure: agent \"{a.name}\" computes with `{CODE_TOOL}`: work that is code over the run's "
                       "data (filtering, totals, a chart's CSV) is a `transform` step whose answer the next step reads, "
                       "or a Python tool; keep an agent for what needs judgement")
        if a.type != "agent" or len(a.tools) != 1 or a.memory or a.knowledge or a.browser or a.servers:
            continue
        if not _reads(a.tools[0], custom):
            continue
        using = [s for s in steps if s.type == "agent" and s.agent == a.name]
        if len(using) != 1:
            continue
        out.append(f"structure: step {using[0].id}: \"{a.name}\" only reads with `{a.tools[0]}`, which needs no model: "
                   f"make it a Tool step calling `{a.tools[0]}`, and have the next step read its output as "
                   f"{{{{ state.{using[0].id} }}}}")
    if "team" in ((asked or {}).get("why") or []) and (team := _a_team(plan, ws, steps)):
        out.append(team)
    out += await _browser_agents(plan, steps)
    return out


#: An address a step's words name (F1301): its host is the site a browser agent works.
_SITE = re.compile(r"\bhttps?://[^\s<>\"'`)\]}]+", re.I)


def covered_by_browser_agent(agent: O.NewAgent, steps: list[O.Step]) -> set[str]:
    """The hosts a new plain agent works with the browser's tools, on steps whose words name them: the agent F1301's
    finding says is a browser agent with those sites; set() for any other. TRI1's F1300 leaves such a step to it."""
    from core.browser.gate import ALLOWED

    if agent.type != "agent" or not any(str(t).split("__")[-1] in ALLOWED for t in agent.tools):
        return set()
    words = " ".join(f"{s.prompt or ''} {s.does or ''}" for s in steps if s.type == "agent" and s.agent == agent.name)
    return {h for u in _SITE.findall(words) if (h := (urlsplit(u).hostname or "").lower())}


async def _kept_sites() -> list[str]:
    """The sites the person keeps a usable session for (the pack's `signed_in_sites`); [] when they cannot be read."""
    try:
        from core import site_sessions
        from core.builder_tools import _acting_caller

        return sorted({r["site"] for r in await site_sessions.listed(_acting_caller()) if r.get("usable")})
    except Exception:  # noqa: BLE001 — no kept sessions read: the finding names none
        return []


async def _browser_agents(plan: O.Plan, steps: list[O.Step]) -> list[str]:
    """New plain agents holding the browser's tools on steps that name a site: each is said as a browser agent with
    that site (F1301), and the person's kept session for it where there is one, in one finding (with F1300)."""
    from core.browser.gate import ALLOWED
    from core.sites import same_site

    out: list[str] = []
    kept: list[str] | None = None
    for a in plan.agents:
        hosts = covered_by_browser_agent(a, steps)
        if not hosts:
            continue
        # In the order the steps name them.
        words = " ".join(f"{s.prompt or ''} {s.does or ''}" for s in steps if s.type == "agent" and s.agent == a.name)
        sites = [h for h in dict.fromkeys((urlsplit(u).hostname or "").lower() for u in _SITE.findall(words))
                 if h in hosts]
        kept = await _kept_sites() if kept is None else kept
        sessions = [k for k in kept if any(same_site(h, k) for h in sites)]
        acts = any(not ALLOWED[str(t).split("__")[-1]].get("read_only") for t in a.tools
                   if str(t).split("__")[-1] in ALLOWED)
        how = ("co_drive, so it types there without stopping for the person; read_only when it only looks" if acts
               else "read_only, as it only looks")
        signed = f" and browser.sessions {json.dumps(sessions)}, the person's kept sign-in" if sessions else ""
        out.append(f"structure: agent \"{a.name}\" works {', '.join(sites)} with the browser's tools: make it a "
                   f"browser agent (type browser) with browser.sites {json.dumps(sites)}{signed} and browser.mode "
                   f"{how}")
    return out


def _a_team(plan: O.Plan, ws: Workspace, steps: list[O.Step]) -> str | None:
    """The finding for new agents that hand work to each other through state with no team holding them (F1284)."""
    teams = {k for i, x in ws.agents.items() if x.get("type") == "delegate" for k in (i, x.get("name"))}
    if any(a.type == "team" for a in plan.agents) or any(s.agent in teams for s in steps if s.agent):
        return None
    plain = {a.name for a in plan.agents if a.type == "agent"}
    if len(plain) < 2:
        return None
    by = {s.id: s.agent for s in steps if s.type == "agent" and s.agent in plain}
    for s in steps:
        if s.id not in by:
            continue
        for read in (m.group(1).split(".")[0] for m in STATE_REF.finditer(one_form(s.prompt or ""))):
            if by.get(read) not in (None, by[s.id]):
                names = " and ".join(f'"{n}"' for n in (by[read], by[s.id]))
                return (f"structure: agents {names} hand their work to each other through state (step {s.id} reads "
                        f"{{{{ state.{read} }}}}): these agents work as a team: make an agent of type team with a lead "
                        "and its members (type team, members: the agents it hands to), and one agent step for it in "
                        "place of theirs")
    return None


def as_notes(found: list[str]) -> list[str]:
    """Structure findings as the card says them, once the planner has had them back."""
    return [f.removeprefix("structure: ") for f in found]


__all__: list[Any] = ["issues", "as_notes", "PAGE_READS", "covered_by_browser_agent"]
