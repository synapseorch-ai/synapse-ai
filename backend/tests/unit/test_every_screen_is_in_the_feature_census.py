"""Every screen of the app is in the feature census: reached by Synapse AI, or said as not from here yet (F1256).

"Create an eval for my curiosity post generator" built a workflow that imitated an eval (TRI1's triage part 3, 10-30):
nothing tied the app's screens to Synapse AI's reach, the capabilities text it reads left out Evals, Boards and Skills,
and a feature it could not reach was rebuilt from workflow parts. The census (`core.feature_census`) is that tie. These
hold it to nav.ts, both rails, read from the file, and to the tools that exist, so it cannot go stale. The
capabilities text and the planner's `not_here` are made from it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

NAV = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "nav.ts"
_ENTRY = re.compile(r"id: '([a-z_]+)', label: '([^']+)', href: '([^']+)'")


def _nav() -> dict[str, tuple[str, str]]:
    """Every screen nav.ts lists (a hub has no href; its tabs are the screens), by id: (label, href)."""
    found = {m.group(1): (m.group(2), m.group(3)) for m in _ENTRY.finditer(NAV.read_text())}
    assert len(found) > 30, "nav.ts was read"
    return found


def _census():
    from core import feature_census

    return feature_census


def test_every_screen_has_a_row_and_every_row_a_screen():
    rows = {s.id: (s.label, s.href) for s in _census().CENSUS}
    nav = _nav()
    assert sorted(set(nav) - set(rows)) == [], "a screen nav.ts lists, with no census row"
    assert sorted(set(rows) - set(nav)) == [], "a census row for a screen nav.ts no longer lists"
    assert {i: rows[i] for i in nav} == nav, "a row's label and address are the screen's"


def test_every_row_says_what_synapse_ai_does_there_or_what_it_does_not():
    for s in _census().CENSUS:
        assert s.tools or s.cant, f"{s.id}: neither reach nor 'not from here yet'"
        assert bool(s.tools) == bool(s.does), f"{s.id}: its tools and what they do go together"


def test_every_tool_a_row_names_exists():
    from core import builder_tools
    from core.builder_readers import TOPICS
    from core.native_builder import build_plan, build_save, change_apply, classify  # noqa: F401 — registers theirs
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    tools = {t.name for t in registry.all_tools()} | set(builder_tools.BUILDER_TOOL_NAMES)
    missing = []
    for s in _census().CENSUS:
        for name in s.tools:
            tool, _, topic = name.partition(":")
            if tool not in tools or (topic and topic not in TOPICS):
                missing.append(f"{s.id}: {name}")
    assert missing == []


@pytest.mark.parametrize("screen", ["Evals", "Boards", "Skills"])
def test_the_capabilities_text_is_the_census(screen):
    """It left out Evals, Boards and Skills by hand; made from the census it cannot."""
    from core import builder_help

    text = builder_help.SELF_HOSTED_CAPABILITIES
    assert f"- {screen}: " in text or f"open {screen} (" in text, "reached, or said with its screen"
    assert builder_help.SELF_HOSTED_CAPABILITIES == _census().capabilities_text()


def test_evals_is_said_with_its_screen_until_synapse_ai_reaches_it():
    evals = next(s for s in _census().CENSUS if s.id == "evals")
    assert evals.tools or {"what": evals.cant, "open": "Evals (/evals)"} in _census().not_here()


def test_the_planner_is_told_never_to_imitate_what_is_not_here():
    from core.native_builder.seed import bundled_definitions

    planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
    assert "`not_here`" in planner["system_prompt"] and "never build it from workflow parts" in planner["system_prompt"]


async def test_part_3_s_eval_request_finds_evals_in_the_pack(client):
    """"Create an eval for my curiosity post generator": the plan route's pack names Evals and its screen."""
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState
    from tests.unit.test_the_build_route_decides_in_code import _call

    run = OrchestrationRun(run_id="builder_f1256", orchestration_id="orch_native_builder", shared_state={
        builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
        "user_message": "create an eval for me curiosity post generator"})
    await SharedState(run).checkpoint()
    pack, _ = await _call("read_plan_context", {}, run)
    assert any(n["open"] == "Evals (/evals)" for n in pack["not_here"])


def test_a_host_s_own_census_is_what_the_planner_reads():
    F = _census()
    mine = (F.Screen("billing", "Billing", "/settings/billing", cant="changing the plan"),)
    F.set_census(mine)
    try:
        assert F.not_here() == [{"what": "changing the plan", "open": "Billing (/settings/billing)"}]
    finally:
        F.set_census(None)


# ── what a person calls a screen, and what imitating it looks like (GUIDE1's F1257, 8a 10-30) ─────────────────────
def _names(census) -> list[tuple[str, str]]:
    return [(n, s.id) for s in census for n in s.names]


def test_every_name_is_one_row_s_alone():
    """GUIDE1's pack finds a screen by what a person calls it: a name two rows share would find either."""
    names = [n.casefold() for n, _ in _names(_census().CENSUS)]
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_no_name_is_another_row_s_id_or_label():
    census = _census().CENSUS
    clash = [f"{row}: {name}" for name, row in _names(census) for s in census
             if s.id != row and name.casefold() in (s.id.casefold(), s.label.casefold())]
    assert clash == []


def test_a_name_is_written_as_a_person_types_it():
    assert [n for n, _ in _names(_census().CENSUS) if not n.strip() or n != n.strip().lower()] == []


def test_evals_is_found_by_its_names_and_says_what_imitating_it_looks_like():
    """Part 3's request ("create an eval") and its kin: test cases, a golden set, a rubric."""
    evals = next(s for s in _census().CENSUS if s.id == "evals")
    assert {"eval", "test cases", "golden set", "rubric"} <= set(evals.names)
    assert "workflow" in evals.imitation and "score" in evals.imitation


def test_an_imitation_is_said_only_where_something_is_not_reached():
    """An imitation is the planner's to name for a screen it cannot fully reach: every row with one has a `cant`."""
    assert [s.id for s in _census().CENSUS if s.imitation and not s.cant] == []


def test_a_host_s_row_keeps_its_names_and_imitation():
    """Cloud's census is the engine's rows under its own ids (`dataclasses.replace`): both fields come along."""
    from dataclasses import replace

    evals = next(s for s in _census().CENSUS if s.id == "evals")
    moved = replace(evals, id="evals_cloud", href="/evals")
    assert (moved.names, moved.imitation) == (evals.names, evals.imitation)
    assert _census().Screen("x", "X", "/x", "", (), "nope").names == (), "the positional form still builds a row"
