"""Evals, Usage and the rest are findable (GUIDE1, D318, F914).

Naveen: "the main menus tabs inside like evals usage all are great features but not visible to users". A tab's page
said its hub's name ("Runs" over Evals), the rail named only the hub, and Home pointed at none of them. His call
(2026-10-23): the page is titled by its tab, the hub's tooltip lists its tabs, and Home's "More you can do" points at
what a workspace hasn't used. `components/run` is synced into cloud's web, so its half holds both products' Home.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"


def test_a_tab_s_page_is_titled_by_the_tab():
    screen = (FRONTEND / "components" / "app" / "AppScreen.tsx").read_text("utf-8")
    assert "title={title ?? entry.label}" in screen
    assert "hub.label : entry.label" not in screen, "a tab's page titled by its hub again"


def test_a_hub_s_tooltip_names_its_tabs():
    rail = (FRONTEND / "components" / "app" / "AppRail.tsx").read_text("utf-8")
    assert "tabs.map(t => t.label).join(' · ')" in rail


def test_more_you_can_do_points_at_real_pages():
    home = (FRONTEND / "components" / "run" / "home.ts").read_text("utf-8")
    table = home[home.index("export const MORE_TO_TRY"):]
    table = table[: table.index("\n};")]
    hrefs = re.findall(r"href: '(/[a-z-]+)'", table)
    assert len(hrefs) == 3  # AI tools has its own Home card since F1027
    for href in hrefs:
        assert (FRONTEND / "app" / "(app)" / href.strip("/") / "page.tsx").exists(), href


def test_a_section_s_first_visit_explains_each_of_its_tabs():
    """F1028, Naveen: "when they land on runs page let's show other tabs and its uses and same way let's do the same
    in other pages explaining each tabs". Built from the nav (every tab, in its own blurb's words), so a new tab can't
    be left out; each tab of the bar carries the anchor its stop needs."""
    tabs = (FRONTEND / "components" / "ui" / "hub-tabs.tsx").read_text("utf-8")
    app_tabs = (FRONTEND / "components" / "app" / "AppHubTabs.tsx").read_text("utf-8")
    tours = (FRONTEND / "lib" / "tours.ts").read_text("utf-8")
    host = (FRONTEND / "lib" / "helpHost.tsx").read_text("utf-8")
    assert "data-tour={tab.id ? `tab-${tab.id}` : undefined}" in tabs
    assert "id: t.id" in app_tabs, "OSS's tab bar names each tab for its stop"
    assert "export function tabTourFor" in tours and "anchor: `tab-${t.id}`" in tours and "body: t.blurb" in tours
    assert "tabTourFor," in host
