"""A knowledge base's note or text file is changed in place, one passage at a time (F1258; Naveen 10-30: "change just a
knowledge base section").

The change route could add, rename, rebuild and delete in a knowledge base, never change what one document says.
Now `knowledge_edit_document` names the document and the passage as it stands (`find`) with its new text
(`replace`), or a short note's whole text (`text`) (Naveen: the exact passage). Code holds it:

* only a note (a record of text) or a text file in its base's folder: a PDF, a Word file or a page read from a site
  is said, with the nearest thing that works;
* the passage found exactly once (none, or two, is a question);
* no key in it, and never a document left empty;
* the review shows the section before and after; the document keeps its name and folder and is indexed again;
* written only over the text the review was made from: changed since, it is said, never written over (TRI1);
* "undo that" writes the text as it was through the same door, so its index is built again from it too (8a).

The planner finds the passage with `search_knowledge` (each hit by its document's id), or a base's documents with
`read_workspace` (knowledge, with the base's name), which now lists them with their ids.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import bases, documents, ingest, seams, sync
from core.knowledge.api_models import KnowledgeBaseCreate, RecordIn, RecordIngestRequest
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role
from core.vault_events import VaultChange

pytestmark = pytest.mark.usefixtures("knowledge_store")

NOTE = ("Refunds: a customer may return an unused kit within 30 days for a full refund.\n"
        "Shipping: kits ship in two working days.")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def world(monkeypatch):
    seams.reset_all()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    jobs = RecordingDispatcher()
    seams.set_ingest_dispatcher(jobs)
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    audits: list = []

    async def audit(event):
        audits.append(event)

    hooks.set_role_source(owner)
    hooks.set_audit(audit)
    kb = await bases.create_base(KnowledgeBaseCreate(name="Support handbook", description="x",
                                                     folder="knowledge/support"))
    [note] = await ingest.put_records(kb, RecordIngestRequest(records=[RecordIn(text=NOTE, title="Refunds policy")]))
    write_vault("knowledge/support/hours.md", "# Hours\n\nOpen 9 to 5, Monday to Friday.\n")
    [hours] = await sync.on_vault_change(VaultChange(path="knowledge/support/hours.md", op="write", source="route"))
    write_vault("knowledge/support/contract.pdf", b"%PDF-1.4 not really")
    [pdf] = await sync.on_vault_change(VaultChange(path="knowledge/support/contract.pdf", op="write", source="route"))
    jobs.jobs.clear()
    yield {"kb": kb, "note": note, "hours": hours, "pdf": pdf, "jobs": jobs, "audits": audits}
    hooks.set_role_source(None)
    hooks.set_audit(None)
    builder_access.forget_runs()
    seams.reset_all()


_N = {"n": 0}


async def _run():
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_kedit_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft, *, before_apply=None):
    """One whole change: check, the review answered yes, approve, apply."""
    run = await _run()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    if before_apply is not None:
        await before_apply()
    return run, checked, await _call("apply_change", {}, run)


def _edit(doc_id, **fields):
    return {"ops": [{"op": "knowledge_edit_document", "target": {"kind": "document", "id": doc_id},
                     "fields": fields}]}


async def _text(row) -> str:
    return (await ingest.editable_text(await documents.get(row.id)))[0]


def _queued(world, row) -> bool:
    return any(j.doc_id == row.id or j.path == row.path for j in world["jobs"].jobs)


class TestOnePassage:
    async def test_a_notes_passage_is_changed_and_indexed_again(self, world):
        _, checked, applied = await _change(_edit(world["note"].id, find="within 30 days",
                                                  replace="within 60 days"))
        assert "before:" in checked["review"] and "[30]" in checked["review"] and "[60]" in checked["review"], \
            checked["review"]
        assert "Refunds policy" in checked["review"] and "Support handbook" in checked["review"]
        assert applied["applied"] == ["knowledge_edit_document"], applied
        assert await _text(world["note"]) == NOTE.replace("within 30 days", "within 60 days")
        row = await documents.get(world["note"].id)
        assert row.title == "Refunds policy" and row.path == world["note"].path, "its name and folder stay"
        assert _queued(world, world["note"]), "indexed again"
        assert any(getattr(a, "action", None) == "knowledge.document.edited" or "knowledge.document.edited" in str(a)
                   for a in world["audits"])

    async def test_a_text_files_passage_is_changed_at_its_own_path(self, world):
        _, _, applied = await _change(_edit(world["hours"].id, find="Open 9 to 5", replace="Open 8 to 6"))
        assert applied["applied"] == ["knowledge_edit_document"], applied
        assert await _text(world["hours"]) == "# Hours\n\nOpen 8 to 6, Monday to Friday.\n"
        assert (await documents.get(world["hours"].id)).path == "knowledge/support/hours.md"
        assert _queued(world, world["hours"])

    async def test_a_short_note_can_be_given_whole(self, world):
        _, _, applied = await _change(_edit(world["note"].id, text="Refunds: within 60 days."))
        assert applied["applied"] == ["knowledge_edit_document"]
        assert await _text(world["note"]) == "Refunds: within 60 days."

    async def test_an_empty_replace_takes_the_passage_out(self, world):
        await _change(_edit(world["note"].id, find="\nShipping: kits ship in two working days.", replace=""))
        assert await _text(world["note"]) == NOTE.split("\n")[0]


class TestWhatIsAskedOrSaid:
    @pytest.mark.parametrize("find, says", [("within 90 days", "which part should change?"),
                                             ("kit", "which one should change?")])
    async def test_a_passage_not_there_once_is_a_question(self, world, find, says):
        _, checked, _ = await _change(_edit(world["note"].id, find=find, replace="x"))
        assert checked["outcome"] == "said" and says in checked["say"], checked

    async def test_a_pdf_is_said_with_what_to_do_instead(self, world):
        _, checked, _ = await _change(_edit(world["pdf"].id, find="x", replace="y"))
        assert "PDF" in checked["say"] and "in its place" in checked["say"], checked

    async def test_a_page_read_from_a_site_is_said(self, world, monkeypatch):
        row = world["hours"]
        await documents.set_columns(row.id, source="web")
        _, checked, _ = await _change(_edit(row.id, find="Open 9 to 5", replace="x"))
        assert "next crawl" in checked["say"], checked

    async def test_a_key_is_never_written(self, world):
        _, checked, _ = await _change(_edit(world["note"].id, find="30 days", replace="sk_live_" + "Z" * 24))
        assert checked["outcome"] == "said" and "Keys never go in a change" in checked["say"]
        assert await _text(world["note"]) == NOTE

    async def test_never_left_empty(self, world):
        _, checked, _ = await _change(_edit(world["note"].id, find=NOTE, replace=" "))
        assert "empty" in checked["say"], checked

    async def test_both_ways_at_once_is_sent_back(self, world):
        run = await _run()
        out = await _call("submit_change", {"ops": _edit(world["note"].id, find="a", replace="b",
                                                          text="c")["ops"]}, run)
        assert "one of those" in json.dumps(out), out


class TestWrittenOnlyOverWhatWasRead:
    async def test_a_section_changed_since_the_review_is_left_as_it_is(self, world):
        async def someone_else_edits():
            await ingest.put_records(world["kb"], RecordIngestRequest(records=[RecordIn(
                id=ingest._record_id_of(world["note"]), text=NOTE + "\nNew line.", title="Refunds policy")]))

        _, _, applied = await _change(_edit(world["note"].id, find="within 30 days", replace="within 60 days"),
                                      before_apply=someone_else_edits)
        assert "section changed since I read it" in json.dumps(applied), applied
        assert await _text(world["note"]) == NOTE + "\nNew line."


class TestUndo:
    async def test_undo_writes_the_text_back_and_its_index_again(self, world):
        first, _, _ = await _change(_edit(world["note"].id, find="within 30 days", replace="within 60 days"))
        world["jobs"].jobs.clear()
        _, checked, undone = await _change({"undo_of": first.run_id})
        assert checked["outcome"] == "review", checked
        assert undone["applied"] == ["knowledge_edit_document"], undone
        assert await _text(world["note"]) == NOTE
        assert _queued(world, world["note"]), "its index is built again from the text as it was"


class TestFindingIt:
    async def test_a_bases_documents_are_listed_with_their_ids(self, world):
        from core.builder_readers import read_workspace, shape_problems

        read = await read_workspace({"topic": "knowledge", "name": "Support handbook"}, None, [])
        docs = {d["title"]: d for d in read["documents"]}
        assert docs["Refunds policy"]["id"] == world["note"].id and docs["Refunds policy"]["kind"] == "note"
        assert docs["Refunds policy"]["editable"] and docs["hours.md"]["editable"]
        assert docs["contract.pdf"]["kind"] == "file" and not docs["contract.pdf"]["editable"]
        assert shape_problems("knowledge", {k: v for k, v in read.items() if k != "topic"}) == []
        assert "documents" not in await read_workspace({"topic": "knowledge"}, None, []), "only for one base"

    async def test_a_document_is_named_by_its_title(self, world):
        _, _, applied = await _change({"ops": [{"op": "knowledge_edit_document",
                                                "target": {"kind": "document", "name": "Refunds policy"},
                                                "fields": {"find": "30 days", "replace": "45 days"}}]})
        assert applied["applied"] == ["knowledge_edit_document"], applied

    async def test_search_names_each_passages_document(self, world, monkeypatch):
        from core.knowledge.api_models import Citation, Hit, SearchResponse
        from core.mcp_server import knowledge as mcp_knowledge

        async def searcher(request, origin):
            # A hit as the search answers one; only what the tool reads of it is filled in.
            hit = Hit.model_construct(label="K1", chunk_id="kch_1", doc_id=world["note"].id, kb_id=world["kb"].id,
                                      content="Refunds: a customer may return an unused kit within 30 days.",
                                      score=0.9, citation=Citation.model_construct(
                                          label="K1", kb_id=world["kb"].id, doc_id=world["note"].id, chunk_id="kch_1",
                                          path=world["note"].path, source="record", title="Refunds policy"))
            return SearchResponse.model_construct(hits=[hit])

        monkeypatch.setattr(mcp_knowledge, "get_knowledge_searcher", lambda: searcher)
        run = await _run()
        out = await _call("search_knowledge", {"query": "refund window"}, run)
        [hit] = out["passages"]
        assert hit["doc_id"] == world["note"].id and hit["document"] == "Refunds policy"
        assert hit["knowledge_base"] == "Support handbook" and "untrusted" in out["note"]

    def test_the_planner_holds_the_search(self):
        # The one planner plans and changes since D340 D0, its rule 12 under # Changing.
        from core.native_builder.seed import bundled_definitions

        planner = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_planner")
        assert "search_knowledge" in planner["tools"] and "knowledge_edit_document" in planner["system_prompt"]


class TestAHostWritesItsOwnWay:
    """F1258's seam (79, 11-02): a host whose Knowledge routes do more (cloud's vault reserve, audit and its own Vault
    marking) writes the edited text its own way through `set_knowledge_host`, on every door; with no `write_text` of
    its own, the engine's `ingest.write_text`, as before. The checks before it (the text read, written only over it,
    the base on) stay the engine's."""

    @pytest.fixture
    def host(self):
        from core.product_tools import knowledge as tools

        calls: list = []

        async def write_text(caller, kb, row, text):
            calls.append((kb.id, row.id, text))
            return await ingest.write_text(kb, row, text)

        tools.set_knowledge_host(tools.KnowledgeHost(create_base=tools._oss_create_base,
                                                     add_vault_file=tools._oss_add_vault_file, write_text=write_text))
        yield calls
        tools.set_knowledge_host(None)

    async def test_an_edit_and_its_undo_are_written_through_the_hosts_write(self, world, host):
        first, _, applied = await _change(_edit(world["hours"].id, find="Open 9 to 5", replace="Open 8 to 6"))
        assert applied["applied"] == ["knowledge_edit_document"], applied
        assert host == [(world["kb"].id, world["hours"].id, "# Hours\n\nOpen 8 to 6, Monday to Friday.\n")]
        await _change({"undo_of": first.run_id})
        assert host[-1][2] == "# Hours\n\nOpen 9 to 5, Monday to Friday.\n" and len(host) == 2, "undo by the same door"

    async def test_what_is_refused_never_reaches_the_host(self, world, host):
        await _change(_edit(world["pdf"].id, text="anything"))
        assert host == []

    def test_a_host_with_no_write_of_its_own_keeps_the_engines(self):
        from core.product_tools import knowledge as tools

        assert tools.KnowledgeHost(create_base=tools._oss_create_base,
                                   add_vault_file=tools._oss_add_vault_file).write_text is None
        assert tools._HOST.write_text is not None
