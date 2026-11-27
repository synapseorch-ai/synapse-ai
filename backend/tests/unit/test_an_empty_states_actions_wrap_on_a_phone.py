"""An empty state's actions wrap inside its box on a phone (F1740).

`EmptyState` (`components/ui/feedback.tsx`) sets its actions in one centred row. With no wrap, two whole buttons
wider than the box ran past it on both sides at 390 wide (the Tools page's "Import from an API description" and "New
tool"). Now the row wraps, so each button stays whole and inside the box. Synced into cloud's web with the rest of
the kit.
"""
from __future__ import annotations

import re
from pathlib import Path

FEEDBACK = Path(__file__).resolve().parents[3] / "frontend/src/components/ui/feedback.tsx"


def test_its_action_row_wraps():
    source = FEEDBACK.read_text("utf-8")
    body = source[source.index("export function EmptyState("):]
    found = re.search(r'\{action && <div className="([^"]+)">\{action\}</div>\}', body)
    assert found, "EmptyState sets its actions in one row"
    classes = found.group(1).split()
    assert {"flex", "flex-wrap", "justify-center"} <= set(classes), classes


SRC = Path(__file__).resolve().parents[3] / "frontend/src"
#: A centred row of buttons: `flex … justify-center` (not a column) holding two or more buttons or an action slot.
#: A row of one button has nothing to wrap.
_ROW = re.compile(r'<div className="([^"]*)">((?:(?!</div>).){0,400}?)</div>', re.S)


def _button_rows() -> list[tuple[str, str]]:
    rows = []
    for path in sorted(SRC.rglob("*.tsx")):
        for m in _ROW.finditer(path.read_text("utf-8")):
            classes, inner = m.group(1).split(), m.group(2)
            if {"flex", "justify-center"} <= set(classes) and "flex-col" not in classes and (
                    inner.count("<Button") >= 2 or re.search(r"\{actions?\}", inner)):
                rows.append((f"{path.relative_to(SRC)}", m.group(1)))
    return rows


def test_every_centred_row_of_buttons_wraps():
    """Tools and the Vault draw their own empty states with the same centred row; each wraps too."""
    rows = _button_rows()
    assert len(rows) >= 3, rows  # EmptyState's, Tools' and the Vault's
    unwrapped = [r for r in rows if "flex-wrap" not in r[1].split()]
    assert not unwrapped, "a centred row of buttons with no flex-wrap runs past its box on a phone: " + "; ".join(
        f'{where}: className="{classes}"' for where, classes in unwrapped)
