"""A compaction moves older tool results to the vault before it summarises anything, and its archive is a file the
agent can read (D339 P3, F1248).

Prod, 10-29 (TRI1's triage of Naveen's runs): an agent crawling synapseorch.com went over 80k characters; Stage 2
summarised the pages it had read to 16k, and it fetched /pricing again — four times in one step. The summary's hint
named the original as "/tmp/synapse-scratch/…/compaction_archives/….txt": written to the pod's disk only, at a path
no tool takes.

Now Stage 0 saves each older observation of size to the vault and leaves its key — no model call, nothing lost —
and Stage 2's archive is written through the vault, named by the key `read_file` takes.
"""

from __future__ import annotations

import json

import pytest

from core.storage import set_blob_store
from core.storage.base import tenant_key

QUESTION = "pick the most surprising feature of synapseorch.com"


class _Pathless:
    """A blob store with no filesystem, like S3: what is written is in the store, or nowhere."""

    def __init__(self):
        self.blobs: dict[str, str] = {}

    def put(self, key, content):
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped, cut = tenant_key(prefix), tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))
    blobs = _Pathless()
    set_blob_store(blobs)
    yield blobs
    set_blob_store(None)


def _page(n: int, chars: int = 12_000) -> str:
    return f"crawl_multiple: page {n} " + "".join(f"[{n}:{i}]" for i in range(chars // 8))


def _ctx(pages: int, newest: str = "NEWEST-OBSERVATION") -> str:
    return (f"User Request: {QUESTION}\n"
            + "".join(f"\n[Turn {i} — Action]\ncrawl_multiple\n\n[Turn {i} — Observation] {_page(i)}\n"
                      for i in range(1, pages + 1))
            + f"\n[Turn 9 — Observation] read_file: {newest}\n")


async def _compact(ctx, history, threshold, **kw):
    from core.compaction import maybe_compact

    return await maybe_compact(ctx, history, {"auto_compact_threshold": threshold}, "m", "cloud", {},
                               "orch_run_d339", "a_d339", pinned_request=QUESTION, **kw)


class TestStageZero:
    async def test_older_results_go_to_the_vault_and_no_model_is_called(self, store, fake_llm):
        ctx = _ctx(4)
        out, hist, archive, stats = await _compact(ctx, [], 40_000)

        # The newest quarter of the threshold stays verbatim: page 4 is in it.
        assert stats["stage"] == "refs" and stats["moved"] == 3 and archive is None
        assert fake_llm.call_count == 0, "Stage 0 is code, never a model call"
        assert out.startswith(f"User Request: {QUESTION}\n")
        assert "NEWEST-OBSERVATION" in out and _page(4) in out
        assert len(out) < 15_000

    async def test_nothing_is_lost(self, store, fake_llm):
        """Each moved result is in the vault word for word, under the key its stub names."""
        ctx = _ctx(3)
        out, *_ = await _compact(ctx, [], 30_000)

        keys = [line.split("nothing was cut: ", 1)[1].split(",", 1)[0]
                for line in out.splitlines() if "nothing was cut: " in line]
        assert len(keys) == 2
        for n, key in enumerate(keys, start=1):
            assert key.startswith("compaction_archives/"), key
            assert store.get(f"vault/{key}").strip() == _page(n)

    async def test_the_agent_reads_one_back_with_read_file(self, store, fake_llm):
        from core import tool_server, vault_tools
        from core.scale.context import set_resource_provider

        class _Provider:
            async def resolve_agents(self):
                return []

            async def resolve_custom_tools(self):
                return []

            async def resolve_mcp_servers(self):
                return []

        out, *_ = await _compact(_ctx(2), [], 20_000)
        key = out.split("nothing was cut: ", 1)[1].split(",", 1)[0]

        async def _noop() -> None:
            return None

        set_resource_provider(_Provider())
        original, tool_server.ready = tool_server.ready, _noop
        try:
            answer = json.loads((await vault_tools.call("read_file", {"path": key}))[0].text)
        finally:
            tool_server.ready = original
            set_resource_provider(None)
        assert answer["content"].startswith("crawl_multiple: page 1 ")

    async def test_the_newest_result_stays_as_it_is(self, store, fake_llm):
        newest = "N" * 9_000
        out, *_ = await _compact(_ctx(4, newest=newest), [], 40_000)
        assert newest in out

    async def test_a_short_result_is_left_where_it_is(self, store, fake_llm):
        ctx = (f"User Request: {QUESTION}\n"
               + "".join(f"\n[Turn {i} — Observation] Tool 'grep' returned:\n" + "o" * 900 + "\n" for i in range(40)))
        out, _h, _a, stats = await _compact(ctx, [], 10_000)
        assert stats["stage"] != "refs"
        assert "nothing was cut" not in out


class TestStageTwosArchive:
    async def test_it_is_in_the_vault_under_a_key_read_file_takes(self, store, fake_llm):
        fake_llm.set_default("SUMMARY: read four pages.")
        history = [{"role": "user", "content": "u" * 40_000}, {"role": "assistant", "content": "a" * 40_000}]
        out, hist, archive, stats = await _compact(_ctx(4), history, 30_000)

        assert stats["stage"] == "llm_summary"
        assert archive.startswith("compaction_archives/") and not archive.startswith("/")
        assert store.get(f"vault/{archive}"), "the archive is not in the store"
        assert f"in the vault: {archive}; read it with read_file" in out
        assert "/tmp" not in out and "synapse-scratch" not in out
