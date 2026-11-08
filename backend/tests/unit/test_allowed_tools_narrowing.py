"""A step's `allowed_tools` narrows its agent's tool set — it never widens it.

`StepConfig.allowed_tools` has carried a "(narrows only)" contract since it was
added, and the config panel's Restrict-tools checkbox promises the same. The
implementation used to substitute: the override was taken verbatim and gated
against the *global* tool aggregate, so a step could hand its agent a tool the
agent was never given — the exact trap that kept the field out of the UI for a
year. `narrow_allowed_tools` is the fix, and this file is what stops it
regressing to substitution: every other test passes either way.
"""

from core.react_engine import narrow_allowed_tools


class TestNoOverride:
    def test_none_means_the_agents_own_tools(self):
        assert narrow_allowed_tools(None, ["scrape_url", "collect_data"]) == ["scrape_url", "collect_data"]

    def test_empty_list_is_no_override_not_a_lockdown(self):
        # [] has always been falsy here; a lockdown-by-empty-list would change
        # the meaning of every definition saved before the UI existed.
        assert narrow_allowed_tools([], ["scrape_url"]) == ["scrape_url"]

    def test_agent_default_all_passes_through(self):
        assert narrow_allowed_tools(None, ["all"]) == ["all"]


class TestNarrowing:
    def test_intersection_when_both_are_explicit(self):
        assert narrow_allowed_tools(
            ["scrape_url", "bash"], ["scrape_url", "collect_data"]
        ) == ["scrape_url"]

    def test_a_tool_the_agent_lacks_is_not_granted(self):
        # The trap this exists to close: the step asked for bash, the agent
        # never had it, and substitution would have handed it over anyway.
        assert narrow_allowed_tools(["bash"], ["scrape_url"]) == []

    def test_agent_with_all_narrows_to_the_override(self):
        assert narrow_allowed_tools(["scrape_url"], ["all"]) == ["scrape_url"]

    def test_override_all_means_everything_the_agent_has(self):
        # An API author writing ["all"] asks for the agent's set, not the
        # catalog's.
        assert narrow_allowed_tools(["all"], ["scrape_url", "collect_data"]) == ["scrape_url", "collect_data"]

    def test_a_stale_name_silently_drops(self):
        # The agent lost collect_data after the step was configured; the step
        # keeps what remains rather than resurrecting the removed tool.
        assert narrow_allowed_tools(
            ["scrape_url", "collect_data"], ["scrape_url"]
        ) == ["scrape_url"]

    def test_the_input_lists_are_not_mutated(self):
        override = ["scrape_url"]
        agent_tools = ["all"]
        narrow_allowed_tools(override, agent_tools)
        assert override == ["scrape_url"]
        assert agent_tools == ["all"]
