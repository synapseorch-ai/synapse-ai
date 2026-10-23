"""Every guided-tour stop has its anchor in the source (GUIDE1, D318, F919/F920).

A tour stop names the part it lights up by a `data-tour` value (`frontend/src/lib/tours.ts`). Screens change; a
stop whose anchor was dropped would just vanish from the tour at run time, so a screen that loses one fails here
instead. An anchor counts when its name is a quoted string in a .tsx file (as `data-tour="…"`, or a kit prop that
sets it, as TABS1's tab strip does); the tour data itself is a .ts file and never counts. The rail stamps
`rail-<hub id>` on every hub, so those resolve against the hubs in `lib/nav.ts`.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"


def _anchors() -> list[str]:
    tours = (FRONTEND / "lib" / "tours.ts").read_text("utf-8")
    return re.findall(r"anchor: '([a-z0-9_-]+)'", tours)


def _tsx_text() -> str:
    return "\n".join(p.read_text("utf-8") for p in FRONTEND.rglob("*.tsx"))


def test_the_tours_have_stops():
    assert len(_anchors()) >= 10


def test_every_stop_s_anchor_is_on_a_screen():
    source = _tsx_text()
    nav = (FRONTEND / "lib" / "nav.ts").read_text("utf-8")
    hubs = set(re.findall(r"id: '([a-z_]+)', label: '[^']+', icon:", nav))
    rail = (FRONTEND / "components" / "app" / "AppRail.tsx").read_text("utf-8")
    missing = []
    for anchor in _anchors():
        if anchor.startswith("rail-"):
            if anchor[len("rail-"):] not in hubs or "data-tour={`rail-${hub.id}`}" not in rail:
                missing.append(anchor)
        elif f"'{anchor}'" not in source and f'"{anchor}"' not in source:
            missing.append(anchor)
    assert not missing, f"tour stops with no anchor on any screen: {missing}"
