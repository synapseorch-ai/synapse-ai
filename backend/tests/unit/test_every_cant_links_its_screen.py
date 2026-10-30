"""A "can't" says its screen with a link to it, made by code from the census (F1265, GUIDE1; D340 R6).

The `cannot` finish was the model's own words, why and the nearest thing that works, so whether a screen was named,
and named right, was luck; the census gave the planner "open Label (/href)" as text. Now the finish names its screen by
the census row's id, and code writes the link from the host's census (`feature_census.link`), so cloud's paths come
from cloud's rows. A finish that names no screen gets no link: a link chosen from the model's own words would be a
guess shown to the person as fact (a0, 10-31). Every census href is a screen of the app, which this file checks for the
self-hosted rows and pro's twin checks for cloud's.
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from core import builder_access, builder_tools, feature_census  # noqa: F401 — the builder's modules, as at boot
from core.native_builder import change_tools, imitation

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "frontend" / "src" / "app" / "(app)"
PLANNER = ROOT / "backend" / "core" / "native_builder" / "agents" / "planner.json"


def _routes() -> list[re.Pattern]:
    """Each page of the app as a pattern on its path: a `[segment]` matches one segment."""
    out = []
    for page in APP.rglob("page.tsx"):
        parts = [p for p in page.parent.relative_to(APP).parts if not (p.startswith("(") and p.endswith(")"))]
        path = "/" + "/".join(parts)
        out.append(re.compile("^" + re.sub(r"\\\[[^/]+?\\\]", "[^/]+", re.escape(path)) + "$"))
    return out


class TestTheCensusLinks:
    def test_every_row_s_href_is_a_screen_of_the_app(self):
        routes = _routes()
        missing = [f"{s.id}: {s.href}" for s in feature_census.CENSUS if not any(r.match(s.href) for r in routes)]
        assert not missing, f"census rows whose href is no page: {missing}"

    def test_a_row_s_link_is_its_label_to_its_href(self):
        evals = next(s for s in feature_census.CENSUS if s.id == "evals")
        assert feature_census.link(evals) == "[Evals](/evals)"

    def test_the_planner_reads_each_cant_with_its_link(self):
        text = feature_census.capabilities_text()
        for s in feature_census.CENSUS:
            if s.cant:
                assert f"open {feature_census.link(s)}" in text, s.id
        assert all(row["open"].startswith("[") and "](/" in row["open"] for row in feature_census.not_here())

    def test_a_host_census_gives_its_own_paths(self, monkeypatch):
        cloud = tuple(dataclasses.replace(s, href="/org" + s.href) if s.id == "evals" else s
                      for s in feature_census.CENSUS)
        monkeypatch.setattr(feature_census, "current", lambda: cloud)
        assert change_tools.cannot_said({"why": "Not here.", "nearest": "Evals does it.", "screen": "evals"}).endswith(
            "Open [Evals](/org/evals).")


class TestTheFinish:
    async def test_it_names_its_screen_by_the_row_s_id(self):
        got = await change_tools.cannot({"why": "Writing a rubric is done on Evals, not from here yet.",
                                         "nearest": "Open Evals to add one.", "screen": "evals"}, None, [])
        assert got == {"finish": "cannot", "why": "Writing a rubric is done on Evals, not from here yet.",
                       "nearest": "Open Evals to add one.", "screen": "evals"}

    async def test_an_unknown_screen_goes_back_with_the_ids(self):
        got = await change_tools.cannot({"why": "Not here.", "nearest": "Somewhere.", "screen": "rubrics"}, None, [])
        assert "error" in got and "rubrics" in got["error"] and "evals" in got["error"]

    def test_its_words_end_with_the_screen_s_link(self):
        said = change_tools.cannot_said({"why": "Writing a rubric is done on Evals, not from here yet.",
                                         "nearest": "Open Evals to add one.", "screen": "evals"})
        assert said == ("Writing a rubric is done on Evals, not from here yet. Open Evals to add one. "
                        "Open [Evals](/evals).")

    def test_no_screen_named_is_no_link_never_a_guess(self):
        # Its words name Evals, but a link is said only for the screen the finish names.
        said = change_tools.cannot_said({"why": "Writing a rubric is done on Evals.", "nearest": "Use Evals."})
        assert "](" not in said and said == "Writing a rubric is done on Evals. Use Evals."

    def test_the_check_says_it(self):
        import inspect

        assert "cannot_said(draft)" in inspect.getsource(change_tools.check_change)

    def test_the_tool_and_the_planner_ask_for_the_screen(self):
        props = change_tools.CANNOT["function"]["parameters"]["properties"]
        assert "screen" in props and "screen" not in change_tools.CANNOT["function"]["parameters"]["required"]
        assert "census" in props["screen"]["description"] or "not_here" in props["screen"]["description"]
        planner = json.dumps(json.loads(PLANNER.read_text("utf-8")), ensure_ascii=False)
        assert "name its `screen`" in planner


class TestOtherSentencesUseTheLink:
    @pytest.mark.parametrize("row", ["templates"])
    def test_an_imitation_s_row_sentence_links_its_screen(self, row, monkeypatch):
        monkeypatch.setattr(imitation, "SIGNATURES", {row: lambda plan, ws: True})
        screen = next(s for s in feature_census.CENSUS if s.id == row)
        assert feature_census.link(screen) in imitation._said(screen)
