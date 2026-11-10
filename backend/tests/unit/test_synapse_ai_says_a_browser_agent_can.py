"""Synapse AI says it has no browser of its own, but a Browser agent does, and offers to make one (F1513, eb's QA B2;
Naveen's pick 11-10).

On prod, asked to use the browser, Synapse AI answered "I can't use a browser to visit websites… outside what I'm built
to do", while the product has Browser agents. Nothing it reads mentioned one: the census's Browser row lists the kept
sign-ins and recordings, the general limits said a service with no API "cannot" be used, with a researching workflow
as the nearest thing, and what the product delivers had no example of work on a website. Now its capabilities say a
Browser agent works a website's own pages, and that Synapse AI offers to make one. They promise nothing the install
may not do: no schedule in the example (cloud's Free has a browser in chat only, its own words in cloud's census).
"""
from __future__ import annotations

import re


def _limits_line(text: str, starts: str) -> str:
    """The general limit that starts with `starts`, as one line."""
    lines = re.split(r"\n- ", text)
    found = [" ".join(line.split()) for line in lines if line.startswith(starts)]
    assert found, (starts, text)
    return found[0]


class TestItSaysABrowserAgentCan:
    def test_it_has_no_browser_of_its_own_and_offers_a_browser_agent(self):
        from core import builder_help

        said = _limits_line(builder_help.capabilities(), "It (Synapse AI) has no browser of its own")
        assert "Nearest: a Browser agent" in said, said
        assert "real web pages" in said and "the person can watch" in said, said
        assert "offer to make one" in said, said

    def test_a_site_with_no_api_is_a_browser_agents(self):
        from core import builder_help

        said = _limits_line(builder_help.capabilities(), "It cannot use a service that offers no API")
        assert "Nearest: a Browser agent" in said, said
        assert "asks the person to book" in said, said  # the researching workflow is still offered

    def test_what_it_delivers_has_work_on_a_website(self):
        from core.feature_census import WHAT_IT_DELIVERS

        browser = [line for line in WHAT_IT_DELIVERS.splitlines() if "Browser agent" in line]
        assert len(browser) == 1, WHAT_IT_DELIVERS
        # Nothing a plan may not have: no schedule, no kept sign-in (cloud's Free has a browser in chat only).
        assert not re.search(r"schedule|each (morning|day|week)|every|signed in|session", browser[0]), browser[0]

    def test_the_planner_reads_the_limit_but_not_the_example(self):
        from core import builder_help
        from core.feature_census import WHAT_IT_DELIVERS

        plain = builder_help.capabilities()
        assert "It (Synapse AI) has no browser of its own" in plain
        browser = next(line for line in WHAT_IT_DELIVERS.splitlines() if "Browser agent" in line)
        assert browser not in plain
