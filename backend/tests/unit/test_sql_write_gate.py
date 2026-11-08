"""What counts as a write, and what counts as one statement.

`run_sql_query` runs model-authored SQL. The gate deciding whether a query may
modify the database used to look at the query's **first word**, which two very
ordinary shapes walk straight past:

    WITH x AS (SELECT 1) DELETE FROM t     -- first word is "with"
    SELECT 1; DROP TABLE t                 -- first word is "select"

Both would have run against a database the operator had marked read-only.
"""
import pytest

from tools.sql_agent import _is_write_query, _statement_count


class TestTheFirstWordWasNotEnough:
    @pytest.mark.parametrize("query", [
        "WITH x AS (SELECT 1) DELETE FROM t",
        "with recent as (select * from orders) update recent set flag = 1",
        "SELECT 1; DROP TABLE t",
        "  \n  insert into t values (1)",
        "COPY t FROM '/etc/passwd'",
        "DO $$ BEGIN PERFORM 1; END $$",
        "CALL some_procedure()",
    ])
    def test_these_are_writes(self, query: str) -> None:
        assert _is_write_query(query) is True


class TestOrdinaryReadsStillPass:
    @pytest.mark.parametrize("query", [
        "SELECT * FROM t",
        "select id, name from customers where active",
        # The word appears in data, not as a keyword.
        "SELECT * FROM t WHERE status = 'delete me'",
        'SELECT * FROM t WHERE note = "update the thing"',
        # …and in comments.
        "-- drop table x\nSELECT 1",
        "/* update */ SELECT 1",
        # …and inside identifiers, which tokenise as one word.
        "SELECT delete_flag, update_count FROM t",
        "select * from public.updates",
        "SELECT 1;",
    ])
    def test_these_are_reads(self, query: str) -> None:
        assert _is_write_query(query) is False


class TestStatementCounting:
    @pytest.mark.parametrize(("query", "expected"), [
        ("SELECT 1", 1),
        ("SELECT 1;", 1),
        ("SELECT 1; DROP TABLE t", 2),
        ("SELECT 1;;", 1),
        # A semicolon inside a literal is not a separator.
        ("SELECT * FROM t WHERE s = 'a;b'", 1),
        # …nor inside a comment.
        ("SELECT 1 -- ; drop table t", 1),
    ])
    def test_counts(self, query: str, expected: int) -> None:
        assert _statement_count(query) == expected
