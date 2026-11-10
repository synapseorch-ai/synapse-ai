"""A recorded click on a checkbox or a radio reads as the words beside it, never as its value (F1500, eb's QA B12).

A checkbox with no label of its own (the-internet.herokuapp.com/checkboxes: `<input type="checkbox"> checkbox 1`) was
named by `el.value`, which a browser defaults to "on": the step read "Click on", in the recording's editor and to a
Browser agent finding the way, and replay asked first for a checkbox named "on", which no page has. Its words now come
from the text beside it, for the step's sentence only: its name stays empty, so replay starts on a rung that can find
it. A button's value is still its name ("Go").
"""
from __future__ import annotations

import pytest

from core.browser.recording import TARGET_JS, Recorder, line_of

PAGE = """<form id="checkboxes">
  <input type="checkbox"> checkbox 1<br>
  <input type="checkbox" checked> checkbox 2
</form>
<p><input type="radio" name="size" id="small"><!-- a comment is not words --><span>Small</span></p>
<p>Large <input type="radio" name="size" id="large"></p>
<p><label><input type="checkbox" id="terms"> I agree</label></p>
<p><input type="checkbox" id="news" aria-label="Send me news"></p>
<p><input type="submit" id="go" value="Go"></p>
"""

#: Each element of PAGE, as a person's click on it reads.
LINES = {
    "#checkboxes > input:nth-of-type(1)": "Click checkbox 1",
    "#checkboxes > input:nth-of-type(2)": "Click checkbox 2",
    "#small": "Click Small",
    "#large": "Click Large",
    "#terms": "Click I agree",
    "#news": "Click Send me news",
    "#go": "Click Go",
}


def test_a_checkbox_with_no_label_reads_as_the_words_beside_it():
    box = {"role": "checkbox", "name": "", "label": "", "text": "", "near": "checkbox 1",
           "css": "#checkboxes > input:nth-of-type(1)", "tag": "input", "type": "checkbox", "autocomplete": "",
           "field": False}
    assert line_of({"action": "click", "target": box}) == "Click checkbox 1"
    rec = Recorder()
    rec.opened("https://the-internet.herokuapp.com/checkboxes")
    step = rec.clicked(box)
    assert step is not None and step["line"] == "Click checkbox 1"


async def _read_targets() -> dict[str, dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("needs Playwright")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"needs Playwright's Chromium: {str(exc)[:160]}")
        try:
            page = await browser.new_page()
            await page.set_content(PAGE)
            return {css: await page.locator(css).evaluate(f"(el) => ({TARGET_JS})(el)") for css in LINES}
        finally:
            await browser.close()


async def test_what_the_page_says_of_each_box_is_its_words_and_never_on():
    targets = await _read_targets()
    for css, line in LINES.items():
        assert line_of({"action": "click", "target": targets[css]}) == line, (css, targets[css])
    for css in ("#checkboxes > input:nth-of-type(1)", "#checkboxes > input:nth-of-type(2)", "#small", "#large"):
        # A choice with no label of its own has no name: replay's role rung would ask for it by a name no page gives.
        assert targets[css]["name"] == "", (css, targets[css])
    assert targets["#news"]["name"] == "Send me news"
    assert targets["#go"]["name"] == "Go", "a button's value is still its name"
