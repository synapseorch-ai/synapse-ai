"""The eager MEMORY index shows the newest memories (F273).

120 memories made a 100-line, 8,805-character MEMORY.md; the prompt block kept
its first 4,096 characters, and an updated or new memory's line goes to the
end, so the 54 newest were the ones hidden. A fact in the visible part came
back in 7 s; the newest (W118) took 44 s of searching.

Now the block keeps the newest lines that fit and says how many older ones
are not shown, and where to find them.
"""
from core import agent_memory as am


def _index(n: int) -> str:
    return "\n".join(f"- [w{i:03d}] Warehouse W{i:03d}: capacity {i * 75}, manager Person {i}"
                     for i in range(n))


class TestTheBlock:
    def test_the_newest_lines_ride_the_prompt(self):
        block = am.render_index_block(_index(100))
        assert "[w099]" in block and "[w098]" in block, "the newest memories are shown"
        assert "[w000]" not in block, "the oldest give way"

    def test_it_says_how_many_are_not_shown_and_where_they_are(self):
        block = am.render_index_block(_index(100))
        shown = sum(1 for line in block.splitlines() if line.startswith("- [w"))
        assert f"{100 - shown} older" in block
        assert "list_memories" in block or "search_memory" in block

    def test_it_stays_within_its_budget(self):
        block = am.render_index_block(_index(100))
        body = block.split("\n", 2)[-1]
        assert len(body) <= 4_096 + 200

    def test_a_short_index_is_whole_and_says_nothing_extra(self):
        block = am.render_index_block(_index(5))
        assert all(f"[w{i:03d}]" in block for i in range(5))
        assert "older" not in block
