"""Which databases an agent may query, and what `"all"` no longer covers.

Two rules that arrived together because they answer the same question from
opposite ends. `OPT_IN_SERVERS` decides whether an agent gets the SQL tool at
all; `_check_db_allowlist` decides which database it reaches once it has it.
"""
from core.react_engine import _check_db_allowlist
from core.tools import OPT_IN_SERVERS, permits


class TestOptInServers:
    def test_all_does_not_cover_an_opt_in_server(self):
        """The change: an agent created by ticking nothing has no SQL."""
        assert permits(["all"], "run_sql_query", opt_in=True) is False

    def test_all_still_covers_everything_else(self):
        assert permits(["all"], "web_scraper") is True

    def test_naming_it_is_what_opting_in_means(self):
        assert permits(["all", "run_sql_query"], "run_sql_query", opt_in=True) is True
        assert permits(["run_sql_query"], "run_sql_query", opt_in=True) is True

    def test_sql_is_the_first_member(self):
        """A guard on the set itself: joining it is a decision about what a
        surprised owner would call a breach, not a tidy-up."""
        assert "sql" in OPT_IN_SERVERS


class TestTheDatabaseAllowlist:
    def test_a_database_the_agent_was_not_given_is_refused(self):
        agent = {"db_configs": ["db_reporting"]}
        error = _check_db_allowlist(agent, "sql", {"db_id": "db_production"})
        assert error and "not permitted" in error
        # And it names what may be used, so the model can correct itself rather
        # than retrying the same call.
        assert "db_reporting" in error

    def test_a_permitted_database_passes(self):
        agent = {"db_configs": ["db_reporting"]}
        assert _check_db_allowlist(agent, "sql", {"db_id": "db_reporting"}) is None

    def test_one_database_is_filled_in_rather_than_demanded(self):
        """With no choice to make, making it is a kindness — and it stops the
        tool falling through to the deployment-wide connection string."""
        agent = {"db_configs": ["db_only"]}
        args = {}
        assert _check_db_allowlist(agent, "sql", args) is None
        assert args["db_id"] == "db_only"

    def test_several_databases_and_no_choice_is_an_error_not_a_guess(self):
        agent = {"db_configs": ["db_a", "db_b"]}
        error = _check_db_allowlist(agent, "sql", {})
        assert error and "required" in error

    def test_an_agent_with_no_list_is_unrestricted(self):
        """Every agent that exists today is in this state. A list defaulting to
        closed would break all of them on upgrade."""
        assert _check_db_allowlist({}, "sql", {"db_id": "anything"}) is None
        assert _check_db_allowlist({"db_configs": []}, "sql", {"db_id": "x"}) is None

    def test_other_servers_are_not_this_guard_s_business(self):
        agent = {"db_configs": ["db_reporting"]}
        assert _check_db_allowlist(agent, "vault", {"db_id": "db_production"}) is None


class TestTheContextIsNoLongerCodeOnly:
    async def test_a_conversational_agent_gets_its_databases_described(self):
        """The gate is the agent's own list now, not `type == "code"` — which
        was the one shape that could never be given a database."""
        from unittest.mock import AsyncMock, patch

        from core.react_engine import _inject_db_context

        configs = [{"id": "db_x", "name": "Reporting", "db_type": "postgres"}]
        with patch(
            "core.routes.db_configs.load_db_configs", new=AsyncMock(return_value=configs)
        ):
            out = await _inject_db_context(
                {"type": "conversational", "db_configs": ["db_x"]}, "BASE"
            )
        assert "LINKED DATABASES" in out
        assert "db_x" in out

    async def test_an_agent_with_no_databases_is_left_alone(self):
        from core.react_engine import _inject_db_context

        assert await _inject_db_context({"type": "conversational"}, "BASE") == "BASE"
