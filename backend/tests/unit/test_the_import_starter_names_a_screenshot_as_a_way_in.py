"""The import starter's words name a screenshot as a way in, now that one is read (F1637's copy).

Sent with a screenshot and nothing pasted under it, the import starter's line is planned from the screenshot (F1637).
Its words did not say so: the reply when nothing was pasted (EXPORT_MISSING) and the panel's note when it holds the
bare line back (IMPORT_LEAD_NOTE) offered the Agents SDK code or a description only. Both now name a screenshot of the
workflow as the third way in, in the same words, and stay true before and after Agent Builder closes (F1636's rules,
held by their own test).

Near misses: the reply still opens with "Nothing was pasted under the line", and both still offer the code and the
person's own words.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.native_builder import export

ROOT = Path(__file__).resolve().parents[3]


def _note() -> str:
    source = (ROOT / "frontend/src/components/assistant/switch.ts").read_text()
    m = re.search(r"export const IMPORT_LEAD_NOTE\s*=\s*'((?:\\.|[^'\\])*)'\s*;", source)
    assert m
    return m.group(1).replace("\\'", "'")


def test_both_name_a_screenshot_of_the_workflow():
    for said in (export.EXPORT_MISSING, _note()):
        assert "attach a screenshot of it" in said, said


def test_both_still_offer_the_code_and_the_persons_own_words():
    for said in (export.EXPORT_MISSING, _note()):
        assert "Agents SDK" in said and "your own words" in said, said


def test_the_reply_still_says_nothing_was_pasted():
    assert export.EXPORT_MISSING.startswith("Nothing was pasted under the line")
