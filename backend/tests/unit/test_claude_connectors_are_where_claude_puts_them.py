"""The connect screens send a person to Claude's connectors where Claude now keeps them (F1565).

Claude moved its connectors: "Navigate to Customize > Connectors", then "+ Add", then "Add custom connector"
(support.claude.com, "Get started with custom connectors using remote MCP", read 11-12). The connect screen still
said "Settings → Connectors → Add custom connector", a menu that no longer leads there. `components/connect/` is
synced into cloud's web, so this holds for both products.

The class: no screen, help text or builder prompt names Claude's connectors under Settings, and every place that
tells a person how to add one names the steps Claude shows. (Synapse's own "Settings → Integrations" is ours and
is not this.)
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONNECT = ROOT / "frontend" / "src" / "components" / "connect"

#: The steps Claude shows, as the screens say them.
WHERE = "Customize → Connectors"
ADD = "“+ Add”, then “Add custom connector”"

#: Claude's connectors under Settings, however the arrow is drawn.
_OLD_PATH = re.compile(r"Settings\s*(?:→|->|›|>|&gt;|&rarr;)\s*Connectors", re.I)


def _read(name: str) -> str:
    return (CONNECT / name).read_text("utf-8")


def test_the_full_screen_says_where_claude_keeps_its_connectors():
    host = _read("ConnectAiTools.tsx")
    assert WHERE in host and "+ Add" in host and "Add custom connector" in host
    assert not _OLD_PATH.search(host)


def test_the_one_click_buttons_name_both_of_claudes_steps():
    """The card's and the docs card's Claude buttons open Claude's connectors and copy the address; what they
    say next is the two clicks Claude asks for there."""
    for name in ("ConnectCard.tsx", "LearnCard.tsx"):
        source = _read(name)
        assert f"In Claude, choose {ADD}" in source, name
        assert "CLAUDE_CONNECTORS" in source, name


def test_nothing_sends_a_person_to_claudes_connectors_under_settings():
    found = []
    for top in (ROOT / "frontend" / "src", ROOT / "frontend" / "scripts", ROOT / "backend"):
        for path in top.rglob("*"):
            if (
                path.suffix not in {".ts", ".tsx", ".js", ".mjs", ".py", ".json", ".md", ".txt"}
                or not path.is_file()
                or {"node_modules", ".next", "tests", "venv", ".venv", "__pycache__"} & set(path.parts)
            ):
                continue
            for n, line in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
                if _OLD_PATH.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{n}")
    assert found == []
