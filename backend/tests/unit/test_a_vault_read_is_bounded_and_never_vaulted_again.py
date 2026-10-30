"""A spilled tool result can be read back in pieces a model can hold, and a read of one is never spilled again (D339 P2).

Prod, 10-29 (TRI1's triage of Naveen's runs of "SynapseOrch Curiosity Post Generator"): a crawl of 19 pages answered
307k characters and was vaulted as pretty JSON, one page's markdown per line. The agent read it back by line ranges;
lines 41-83 were 113k characters, so that read was vaulted too, `read_json` could only slice the root's keys, and a
search answered 168k. Ten calls later the step timed out at 300 s.

Here: a list of documents is vaulted a document a file with an index, nothing dropped; every reader answers at most
one read's cap, under the spill line, with where to read on; a JSON file is read by a path into it.
"""

from __future__ import annotations

import json

import pytest

from core.config import default_settings, set_settings_provider
from core.scale.context import set_resource_provider
from core.storage import set_blob_store
from core.storage.base import tenant_key
from core.tenancy import tenant_scope


class _Pathless:
    """A blob store with no filesystem, like S3 (as `test_vault_backend`'s): the fleet's vault, whose references
    are keys relative to the vault."""

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


class _Provider:
    async def resolve_agents(self):
        return []

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def settings():
    s = default_settings()
    s["vault_enabled"] = True
    s["vault_threshold"] = 100_000
    set_settings_provider(lambda: s)
    yield s
    set_settings_provider(None)


@pytest.fixture
def store(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))
    blobs = _Pathless()
    set_blob_store(blobs)
    set_resource_provider(_Provider())
    yield blobs
    set_resource_provider(None)
    set_blob_store(None)


def _crawl(pages: int = 19, chars: int = 15_000) -> dict:
    """crawl_multiple's answer as prod saw it: the pages under `results`, each with its markdown and its links."""
    return {
        "success": True, "count": pages, "succeeded": pages, "failed": 0,
        "results": [{
            "url": f"https://synapseorch.com/page-{i}/",
            "success": True,
            "markdown": f"# Page {i}\n" + ("Approvals by email need no seat. " * (chars // 33)),
            "links": [{"href": f"https://synapseorch.com/x{j}", "text": "a link"} for j in range(30)],
        } for i in range(pages)],
    }


async def _vault_call(name: str, arguments: dict) -> dict:
    from core import tool_server, vault_tools

    async def _noop() -> None:
        return None

    original, tool_server.ready = tool_server.ready, _noop
    try:
        result = await vault_tools.call(name, arguments)
    finally:
        tool_server.ready = original
    return json.loads(result[0].text)


class TestAListOfDocumentsIsVaultedADocumentAFile:
    def test_each_page_is_its_own_file_and_the_reference_is_the_index(self, store):
        from core.vault import maybe_vault

        crawl = _crawl()
        with tenant_scope("org-a"):
            ref = json.loads(maybe_vault("crawl_multiple", json.dumps(crawl)))
            index = json.loads(store.get("vault/" + ref["vault_file"]))
            first = store.get("vault/" + index["documents"][3]["file"])

        assert ref["file_type"] == "documents" and ref["count"] == 19
        assert ref["vault_file"].endswith("/index.json")
        assert first == crawl["results"][3]["markdown"]
        assert index["documents"][3]["url"] == "https://synapseorch.com/page-3/"
        assert index["documents"][3]["chars"] == len(crawl["results"][3]["markdown"])
        # The reference is small: the model reads the index and picks.
        assert len(json.dumps(ref)) < 6_000

    def test_nothing_is_dropped(self, store):
        """Each item's other fields are kept, its text replaced by the file that holds it."""
        from core.vault import maybe_vault

        crawl = _crawl()
        with tenant_scope("org-a"):
            ref = json.loads(maybe_vault("crawl_multiple", json.dumps(crawl)))
            items = json.loads(store.get("vault/" + ref["items_file"]))
            rebuilt = [{**it, "markdown": store.get("vault/" + it["markdown"]["file"])} for it in items]

        assert rebuilt == crawl["results"]
        assert ref["fields"] == {"success": True, "count": 19, "succeeded": 19, "failed": 0}

    def test_other_json_is_one_file_with_its_outline(self, store):
        from core.vault import maybe_vault

        doc = {"meta": {"n": 1}, "rows": [{"a": "x" * 500} for _ in range(400)]}
        with tenant_scope("org-a"):
            ref = json.loads(maybe_vault("list_rows", json.dumps(doc)))

        assert ref["file_type"] == "json"
        assert ref["outline"]["rows"].startswith("list of 400")
        assert "json_path" in ref["message"]


class TestEveryReadIsBounded:
    def test_a_page_on_one_line_is_read_in_pieces_and_nothing_is_lost(self):
        from core.vault import slice_lines

        line = "".join(f"{i:06d}" for i in range(20_000))  # 120k characters, one line
        text, got, start_char = f"head\n{line}\ntail", [], 0
        for _ in range(20):
            part = slice_lines(text, 2, 2, max_chars=20_000, start_char=start_char)
            assert len(part["content"]) <= 20_000
            got.append(part["content"])
            if "next" not in part:
                break
            assert part["next"]["start_line"] == 2
            start_char = part["next"]["start_char"]
        assert "".join(got) == line and len(got) == 6

    def test_a_range_of_lines_is_cut_where_the_cap_falls(self):
        from core.vault import slice_lines

        text = "\n".join("l" * 900 for _ in range(10))
        part = slice_lines(text, 1, 10, max_chars=2_000)
        assert part["content"] == "l" * 900 + "\n" + "l" * 900 + "\n" + "l" * 198
        assert part["next"] == {"start_line": 3, "start_char": 198}

    def test_without_a_cap_a_slice_is_as_it_was(self):
        from core.vault import slice_lines

        assert slice_lines("a\nb\nc\nd", 2, 3) == {"start_line": 2, "end_line": 3, "total_lines": 4,
                                                   "content": "b\nc"}

    def test_a_search_shows_a_long_line_around_its_match(self):
        from core.vault import SEARCH_LINE_CAP, search_text

        line = "x" * 50_000 + "needle" + "y" * 50_000
        found = search_text(line, "needle", 0)
        assert "needle" in found["results"][0]["match"]
        assert len(found["results"][0]["match"]) <= SEARCH_LINE_CAP + 2

    def test_a_read_cap_is_under_the_spill_line(self, settings):
        from core.vault import read_cap

        settings["vault_threshold"] = 30_000
        assert read_cap() == 15_000
        assert read_cap(500_000) == 15_000
        assert read_cap(4_000) == 4_000


class TestTheVaultServerReadsInPieces:
    @pytest.mark.anyio
    async def test_a_read_of_a_spill_is_never_spilled_again(self, store):
        from core.vault import maybe_vault

        line = "z" * 113_000
        with tenant_scope("org-a"):
            store.put("vault/tool_outputs/crawl.json", "[\n" + json.dumps(line) + "\n]")
            raw = await _vault_call("read_file", {"path": "tool_outputs/crawl.json", "start_line": 2,
                                                   "end_line": 2})
            again = maybe_vault("read_file", json.dumps(raw))

        assert len(raw["content"]) <= 20_000 and raw["next"]["start_line"] == 2
        assert json.loads(again) == raw, "a vault read was vaulted again"

    @pytest.mark.anyio
    async def test_read_json_goes_to_a_part_by_its_path(self, store):
        crawl = _crawl(pages=5, chars=60_000)
        with tenant_scope("org-a"):
            store.put("vault/tool_outputs/crawl.json", json.dumps(crawl, indent=2))
            page = await _vault_call("read_json", {"path": "tool_outputs/crawl.json",
                                                    "json_path": "results[3].markdown"})
            more = await _vault_call("read_json", {"path": "tool_outputs/crawl.json",
                                                    "json_path": "results[3].markdown",
                                                    "offset": page["next_offset"]})
            root = await _vault_call("read_json", {"path": "tool_outputs/crawl.json", "json_path": "results"})

        text = crawl["results"][3]["markdown"]
        assert page["root"] == "text" and page["value"] == text[:20_000]
        assert more["value"] == text[20_000:40_000]
        # A page too big to show whole comes back as its outline, to be read by its own path.
        assert root["items"][0]["read_it_with"] == "results.0"
        assert len(json.dumps(root)) <= 22_000

    @pytest.mark.anyio
    async def test_a_wrong_path_says_what_is_there(self, store):
        with tenant_scope("org-a"):
            store.put("vault/tool_outputs/a.json", json.dumps({"results": [1, 2]}))
            answer = await _vault_call("read_json", {"path": "tool_outputs/a.json", "json_path": "pages[0]"})
        assert "error" in answer and "results" in answer["error"]

    @pytest.mark.anyio
    async def test_a_search_answer_fits_one_read(self, store):
        with tenant_scope("org-a"):
            for n in range(40):
                store.put(f"vault/tool_outputs/p{n}.md", "\n".join(f"url {i} " + "w" * 300 for i in range(20)))
            found = await _vault_call("search_files", {"query": "url", "prefix": "tool_outputs/"})
        assert len(json.dumps(found)) <= 22_000
        assert found["truncated"] is True


class TestThePlainInstallsReaderIsCappedToo:
    def test_read_file_by_lines_cuts_and_says_where_to_go_on(self, tmp_path, settings):
        from tools.file_reader import _read_lines

        path = tmp_path / "out.json"
        path.write_text("{\n" + "q" * 90_000 + "\n}")
        part = _read_lines(str(path), 2, 2, roots=[str(tmp_path)])
        assert len(part["content"]) == 20_000
        assert part["next"] == {"start_line": 2, "start_char": 20_000}
        on = _read_lines(str(path), 2, 2, roots=[str(tmp_path)], start_char=20_000)
        assert on["content"] == "q" * 20_000
