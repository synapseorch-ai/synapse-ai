"""Replaying a recording by code (BRW1, D328, F1143; DESIGN §v5.3): what a step puts in, which recorded presses may
run, and what a recording learns while the person does the task — the page a step leads to and where it sent."""
from __future__ import annotations

from core.browser import gate, replay
from core.browser.recording import Recorder, line_of, page_of

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "text": "", "css": "#order",
         "tag": "input", "type": "text", "autocomplete": "", "field": True}
SEARCH = {"role": "button", "name": "Search", "label": "", "text": "Search", "css": "#go", "tag": "button",
          "type": "submit", "autocomplete": "", "field": False}


def test_a_step_puts_in_its_fixed_value_or_the_runs_input_and_never_the_persons():
    assert replay.fill_value({"value": {"fixed": "A-1"}}, {}) == "A-1"
    assert replay.fill_value({"value": {"input": "order"}}, {"order": "B-7"}) == "B-7"
    assert replay.fill_value({"value": {"input": "order"}}, {"order": 1007}) == "1007"
    assert replay.fill_value({"value": {"input": "order"}}, {}) is None, "a missing input stops the step"
    assert replay.fill_value({"value": {"person": "password"}}, {"password": "x"}) is None, "a person's step is theirs"


def test_a_recorded_send_runs_with_someone_there_and_alone_only_when_its_step_may():
    assert replay.press_rule({}, gate.FREE, unattended=True) is None
    assert replay.press_rule({}, gate.ACTS, unattended=False) is None, "the person pressed it when recording"
    assert replay.press_rule({}, gate.ACTS, unattended=True), "nobody there and the step may not send on its own"
    assert replay.press_rule({"may_send": True}, gate.ACTS, unattended=True) is None


def test_a_payment_a_permanent_delete_or_closing_an_account_is_always_the_persons():
    for unattended in (False, True):
        assert replay.press_rule({"may_send": True}, gate.MONEY, unattended=unattended), \
            "recorded or not, allowed to send or not"


def test_the_ladder_tries_role_label_text_then_css():
    class Page:
        def get_by_role(self, role, name, exact):
            return ("role", role, name)

        def get_by_label(self, label, exact):
            return ("label", label)

        def get_by_text(self, text, exact):
            return ("text", text)

        def locator(self, css):
            return ("css", css)

    assert [r for r, _ in replay.locators(Page(), {**ORDER, "text": "Order"})] == ["role", "label", "text", "css"]
    assert [r for r, _ in replay.locators(Page(), {"css": "#x"})] == ["css"]
    assert replay.locators(Page(), None) == []


def test_a_step_that_leads_to_another_page_waits_for_it_and_one_that_stays_does_not():
    rec = Recorder()
    rec.at("about:blank")
    rec.opened("https://portal.example.com/")
    rec.at("https://portal.example.com/")
    rec.clicked(ORDER)
    rec.typed("A-1001", None)
    rec.at("https://portal.example.com/")
    rec.clicked(SEARCH)
    rec.at("https://portal.example.com/results?q=A-1001")  # the query holds what was typed: not in the wait
    rec.went_back()
    open_, typed, search, back = rec.steps
    assert open_["wait"] is None and typed["wait"] is None and back["wait"] is None
    assert search["wait"] == {"for": "url", "url": "https://portal.example.com/results"}
    assert "A-1001" not in str(search["wait"])


def test_the_last_step_learns_its_page_when_the_recording_stops():
    rec = Recorder()
    rec.at("https://portal.example.com/")
    rec.clicked(SEARCH)
    done = rec.result("https://portal.example.com/results")
    assert done["steps"][0]["wait"] == {"for": "url", "url": "https://portal.example.com/results"}


def test_a_recording_keeps_where_each_step_sent_and_never_what():
    rec = Recorder()
    rec.clicked(SEARCH)
    rec.sent("post", "api.example.com")
    rec.sent("POST", "api.example.com")
    rec.sent("PUT", "files.example.com")
    assert rec.steps[0]["sends"] == ["POST api.example.com", "PUT files.example.com"]
    Recorder().sent("POST", "nowhere.example.com")  # nothing recorded yet: nothing to hang it on


def test_a_page_is_its_scheme_host_and_path():
    assert page_of("https://a.example.com/x/y?q=1#top") == "https://a.example.com/x/y"
    assert page_of("https://a.example.com") == "https://a.example.com/"


def test_a_read_step_reads_as_what_it_returns():
    assert line_of({"action": "read", "target": {"label": "Orders"}, "output": "orders"}) == "Read Orders → orders"
