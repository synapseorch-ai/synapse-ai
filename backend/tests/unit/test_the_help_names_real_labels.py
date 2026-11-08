"""Every label the "?" panel quotes is one the screens really show (GUIDE1, D318, F922).

The help's steps name buttons and fields in quotes ("click “Save pricing”"). They drifted: Usage's help said
“Total Cost” and “Add model”, which the screen never had, and a rename on a screen never reached its help. So a
quoted label must appear in the frontend's own source (its .tsx and .ts, not the help itself). A label ending in an
ellipsis (“Make a key for …”) matches its start.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
HELP = FRONTEND / "lib" / "help.ts"


def _normal(text: str) -> str:
    text = text.replace("&apos;", "'").replace("’", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text)


def _screens() -> str:
    files = [p for p in FRONTEND.rglob("*.ts*") if p.suffix in (".ts", ".tsx") and p != HELP]
    return _normal("\n".join(p.read_text("utf-8") for p in files))


def test_every_quoted_label_is_on_a_screen():
    help_text = HELP.read_text("utf-8")
    screens = _screens()
    missing = set()
    for quoted in re.findall(r"“([^”]+)”", help_text):
        label = _normal(quoted).rstrip("…").strip()
        if label and label not in screens:
            missing.add(quoted)
    assert not missing, f"the help quotes labels no screen shows: {sorted(missing)}"
