"""A settings change's row is named by its line's first line, never by its settings (F1332, REACH1; TRI1 and eb 11-04).

The card names a settings change by its line's heading and shows its settings as rows (F1264, plan.ts
`planFromChange`). The heading was the line up to a colon that ends a line, so an undo's line, "Change the workspace's
settings: undo: as they were" and then a line per setting, had none: its name was the whole line, every setting said
again above the rows. These pin the class:

- the backend writes a settings change's line, an undo's included, with its settings on the lines under its first;
- the card takes the name from that first line (F1308's `first`), up to its first " — ", a trailing colon dropped, so
  its settings are only its rows. Read from the source, as F1308's card is (test_a_change_shows_what_was_asked_beside_it).
"""
from __future__ import annotations

from pathlib import Path

ROWS = [{"label": "Default model", "before": "gpt-4.1", "after": "gpt-4.1-mini"},
        {"label": "Prompt caching", "before": "off", "after": "on"}]


class TestTheLine:
    def test_its_settings_are_under_its_first_line(self):
        from core.native_builder.change_tools import _settings_words

        for says in ("", "undo: as they were"):
            first, *under = _settings_words(ROWS, says).split("\n")
            for row in ROWS:
                assert row["label"] not in first, (says, first)
                assert any(row["label"] in line for line in under), (says, under)


class TestTheCard:
    ROOT = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant"

    def test_its_name_is_the_first_lines_heading(self):
        plan_ts = (self.ROOT / "plan.ts").read_text()
        build = plan_ts[plan_ts.index("export function planFromChange"):plan_ts.index("// ── A build, from its own object")]
        [heading] = [line.strip() for line in build.splitlines() if "heading" in line and "=" in line
                     and not line.strip().startswith("//")]
        assert "first" in heading and "o.line" not in heading, heading
        assert ".split(' — ')[0]" in heading and ".replace(/:\\s*$/, '')" in heading, heading
        assert "rows ? heading : what" in build, "a change with rows is named by the heading, its rows shown below"
