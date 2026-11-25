"""A page header's actions wrap inside the row on a phone, so no page scrolls sideways (F1740).

Every app page's title row is `PageHeader` (`components/ui/text.tsx`): the title, then a cluster of the help "?" and
the page's own actions. The cluster was `shrink-0`, which makes it as wide as all its buttons on one line, so its own
`flex-wrap` never wrapped them: at 390 wide the Tools page's three ran past the screen's edge and the page scrolled
sideways. Now the cluster is never wider than its row (`min-w-0 max-w-full`), and its buttons wrap under the title,
each still a whole button. The same component is synced into cloud's web, so its pages wrap the same way.
"""
from __future__ import annotations

import re
from pathlib import Path

TEXT = Path(__file__).resolve().parents[3] / "frontend/src/components/ui/text.tsx"


def _cluster_classes() -> list[str]:
    """The class list of PageHeader's actions cluster: the element holding the help slot and the actions."""
    source = TEXT.read_text("utf-8")
    header = source[source.index("export function PageHeader("):source.index("export function Heading(")]
    found = re.search(r'<div className="([^"]+)">\s*<PageHeaderSlot />\s*\{actions\}', header)
    assert found, "PageHeader's actions cluster holds the help slot, then the actions"
    return found.group(1).split()


def test_the_actions_cluster_is_never_wider_than_its_row():
    classes = _cluster_classes()
    assert "shrink-0" not in classes, "a cluster that cannot shrink is as wide as its buttons on one line"
    assert {"min-w-0", "max-w-full"} <= set(classes), classes


def test_its_buttons_still_wrap_and_an_empty_cluster_still_hides():
    assert {"flex", "flex-wrap", "empty:hidden"} <= set(_cluster_classes())
