"""
The browser tab names the product, SynapseOrch — not the assistant inside it (F829).

`frontend/src/app/layout.tsx`'s metadata said "Synapse AI" for both the title and
the description, while the app's rail reads SynapseOrch; "Synapse AI" is the
assistant's name (ASSIST1, D291), and stays on everything that is the assistant.
"""
from __future__ import annotations

import re
from pathlib import Path

LAYOUT = Path(__file__).resolve().parents[3] / "frontend" / "src" / "app" / "layout.tsx"


def _metadata() -> str:
    m = re.search(r"export const metadata: Metadata = \{(.*?)\n\};", LAYOUT.read_text(encoding="utf-8"), re.S)
    assert m, "layout.tsx has no metadata block"
    return m.group(1)


def test_the_tab_title_is_the_products_name():
    assert re.search(r'^\s*title: "SynapseOrch",$', _metadata(), re.M)


def test_the_description_says_what_it_is_not_the_assistants_name():
    m = re.search(r'^\s*description:\s*"([^"]+)",$', _metadata(), re.M)
    assert m and "Synapse AI" not in m.group(1) and "SynapseOrch" not in m.group(1)
    assert m.group(1) == "AI automation you can read, approve and rely on."
