"""A change is shown the same way wherever Synapse AI asks for a yes: one row a field, as it stands and as it will be
(F1262; TRI1's one shape, 10-30, for F1258's section, F1262's widget and F1264's setting)."""
from __future__ import annotations

from core.native_builder import rows as R


def test_only_what_differs_is_a_row_in_the_labels_order():
    before = {"greeting": "Hello", "enabled": True, "allowed_origins": ["https://a.com"], "theme": "auto"}
    after = {"theme": "auto", "allowed_origins": ["https://a.com", "https://b.com"], "enabled": False}
    labels = {"enabled": "On", "allowed_origins": "Sites", "theme": "Theme", "greeting": "Greeting"}
    assert R.changed(before, after, labels) == [
        {"label": "On", "before": "on", "after": "off"},
        {"label": "Sites", "before": "https://a.com", "after": "https://a.com, https://b.com"},
    ]


def test_something_new_has_no_before_and_nothing_is_no_row():
    assert R.changed(None, {"name": "Support", "title": ""}, {"name": "Name", "title": "Title"}) == [
        {"label": "Name", "before": None, "after": "Support"}]
    assert R.changed({"tools": []}, {"tools": ["x"]}, {"tools": "Tools"}) == [
        {"label": "Tools", "before": "none", "after": "x"}]


def test_a_short_value_is_one_line_a_long_one_two_with_its_change_marked():
    text = R.as_text([{"label": "Greeting", "before": "Hello", "after": "Welcome"},
                      {"label": "Name", "before": None, "after": "Support"},
                      {"label": "Logo", "before": "set", "after": None, "note": "The launcher shows the bubble."}])
    assert text.splitlines() == ["  Greeting: Hello → Welcome", "  Name: Support", "  Logo: set → (none)",
                                 "    The launcher shows the bubble."]
    keep = "Refunds are made to the card that paid, " * 3
    long = R.as_text([{"label": "the section", "before": keep + "within 30 days of the order.",
                       "after": keep + "within 60 days of the order."}])
    was, now = long.splitlines()
    assert was.startswith("  the section, before: …") and "[30]" in was
    assert now.startswith("  the section, after: …") and "[60]" in now


def test_a_value_already_marked_is_shown_as_it_is():
    marked = R.as_text([{"label": "the section", "before": "…within [30 days] for " + "x " * 50,
                         "after": "…within [60 days] for " + "x " * 50}])
    assert "[30 days]" in marked and "[[" not in marked
