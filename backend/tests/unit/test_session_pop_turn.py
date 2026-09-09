"""
The last turn can be taken back.

The session store was append-only: `append_turn` and `delete_one`, nothing
between. Retry and edit-and-resend both need to remove what is there before
sending again, and they must remove a *pair* — `core.session._turns` walks the
list two at a time and stops at the first entry that does not pair, so a
dangling user message silently truncates every turn after it out of the history
the model is given.
"""
import pytest

from core.session import _turns
from core.store import sessions as store


def _pair(u, a):
    return [{"role": "user", "content": u},
            {"role": "assistant", "content": a, "tools": [], "timestamp": "t"}]


@pytest.fixture
async def a_session():
    """Two turns. The store is isolated per test by an autouse fixture."""
    await store.append_turn("s1", "default", _pair("one", "answer one"))
    await store.append_turn("s1", "default", _pair("two", "answer two"))
    return "s1"


class TestPoppingATurn:
    async def test_it_removes_both_halves(self, a_session):
        removed = await store.pop_last_turn("s1", "default")
        assert removed == {"user": "two", "assistant": "answer two"}
        row = await store.get("s1", "default")
        assert [m["content"] for m in row["messages"]] == ["one", "answer one"]

    async def test_what_is_left_still_reconstructs(self, a_session):
        """The failure this guards: a half-removed turn is not a tidiness
        problem, it is a history that stops early."""
        await store.pop_last_turn("s1", "default")
        row = await store.get("s1", "default")
        assert len(_turns(row["messages"])) == 1

    async def test_popping_twice_empties_it(self, a_session):
        await store.pop_last_turn("s1", "default")
        await store.pop_last_turn("s1", "default")
        row = await store.get("s1", "default")
        assert row["messages"] == []

    async def test_an_empty_session_is_not_an_error(self):
        await store.append_turn("s2", "default", [])
        assert await store.pop_last_turn("s2", "default") is None

    async def test_a_session_that_does_not_exist_is_not_an_error(self):
        assert await store.pop_last_turn("nope", "default") is None

    async def test_an_unpaired_tail_is_left_alone(self):
        """Something else wrote a shape we did not: guess at it and the guess
        is a deletion."""
        await store.append_turn("s3", "default", [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "system", "content": "written by something else"},
        ])
        assert await store.pop_last_turn("s3", "default") is None
        row = await store.get("s3", "default")
        assert len(row["messages"]) == 3

    async def test_it_does_not_touch_another_session(self, a_session):
        await store.append_turn("other", "default", _pair("theirs", "their answer"))
        await store.pop_last_turn("s1", "default")
        row = await store.get("other", "default")
        assert len(row["messages"]) == 2
