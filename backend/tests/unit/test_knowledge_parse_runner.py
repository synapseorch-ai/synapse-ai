"""The parsing subprocess (A2, D162): a timeout and a memory cap, a cut that keeps what was read
and says why, refusals carried back typed, and an environment that holds no secret.

Misbehaviour comes from a fake child (`_fakes/knowledge_misbehaving_child.py`)
started through `run_parse(command=…)`; the real child parses real files.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import (
    FakeIndexer, enable_knowledge, install_fake_indexing, use_saved_settings, write_vault,
)
from core.knowledge import bases, dispatch, documents
from core.knowledge.api_models import KnowledgeBaseCreate
from core.knowledge.jobs import run_ingest_job
from core.knowledge.parse import UnsupportedFormat, limits, service
from core.knowledge.parse.limits import ParseRefused, ParserFailed
from core.knowledge.parse.runner import child_env, run_parse
from core.knowledge.types import IngestJob

FAKE_CHILD = [sys.executable, str(Path(__file__).resolve().parents[1] / "_fakes" / "knowledge_misbehaving_child.py")]
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _job(path: str, data: bytes, **more) -> dict:
    return {"path": path, "source": "vault", "sha256": hashlib.sha256(data).hexdigest(), "mime": "x",
            "max_pages": 2000, "ocr": False, "docling": False, **more}


class TestTheRealChild:
    async def test_a_file_parses_in_a_subprocess(self):
        data = b"# Title\n\nBody text."
        async with run_parse(data, _job("kb/a.md", data), timeout_s=60, memory_mb=1024) as out:
            assert out.parsed.title == "Title" and out.parsed.blocks[-1].text == "Body text."
            assert out.cut is None and out.duration_ms > 0 and out.workdir.exists()
        assert not out.workdir.exists()  # the directory goes when the caller is done

    async def test_a_refusal_comes_back_typed(self):
        data = OLE + "EncryptionInfo".encode("utf-16-le")
        with pytest.raises(ParseRefused) as raised:
            async with run_parse(data, _job("kb/secret.docx", data), timeout_s=60, memory_mb=1024):
                pass
        assert (raised.value.status, raised.value.reason) == ("failed", "encrypted")
        with pytest.raises(UnsupportedFormat):
            async with run_parse(b"MZ", _job("kb/a.exe", b"MZ"), timeout_s=60, memory_mb=1024):
                pass


class TestACutIsNeverSilent:
    async def _run(self, fake: str, *, timeout_s: float = 30, memory_mb: int = 1024):
        async with run_parse(b"x", _job("kb/a.pdf", b"x", fake=fake), timeout_s=timeout_s, memory_mb=memory_mb,
                             command=FAKE_CHILD) as out:
            return out.parsed, out.cut

    async def test_a_timeout_keeps_the_pages_read_and_says_so(self):
        clock = time.monotonic()
        parsed, cut = await self._run("sleep", timeout_s=2)
        assert time.monotonic() - clock < 15
        assert cut == "parser_timeout" and parsed.blocks[0].text == "Page one was read."
        assert parsed.stats.truncated_reason == "truncated_pages"
        assert [w.code for w in parsed.warnings][-2:] == ["truncated_pages", "parser_timeout"]

    async def test_memory_past_the_cap_is_stopped(self):
        parsed, cut = await self._run("allocate", memory_mb=200)
        assert cut == "parser_memory" and "200 MB" in parsed.warnings[-1].message

    async def test_a_crash_after_progress_keeps_it(self):
        parsed, cut = await self._run("crash")
        assert cut == "parser_crashed" and "the parser fell over" in parsed.warnings[-1].message

    async def test_a_crash_with_nothing_read_fails_with_its_cause(self):
        with pytest.raises(ParserFailed) as raised:
            await self._run("crash_early")
        assert raised.value.reason == "parser_crashed" and "exit 3" in str(raised.value)

    async def test_a_timeout_with_nothing_read_fails(self):
        with pytest.raises(ParserFailed) as raised:
            async with run_parse(b"x", _job("kb/a.pdf", b"x", fake="crash_early"), timeout_s=0.001,
                                 memory_mb=1024, command=[sys.executable, "-c", "import time; time.sleep(30)"]):
                pass
        assert raised.value.reason == "parser_timeout"

    async def test_a_cancelled_parse_leaves_no_process_behind(self):
        import psutil

        before = {p.pid for p in psutil.Process().children(recursive=True)}
        task = asyncio.ensure_future(self._run("sleep", timeout_s=60))
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert {p.pid for p in psutil.Process().children(recursive=True)} - before == set()


class TestTheEnvironmentHoldsNoSecret:
    def test_only_allowlisted_names_reach_the_child(self, monkeypatch):
        for name in ("DATABASE_URL", "REDIS_URL", "MODEL_GATEWAY_SIGNING_KEY", "AWS_SECRET_ACCESS_KEY",
                     "OPENAI_API_KEY", "SYNAPSE_DB_URL", "SYNAPSE_INTERNAL_TOKEN"):
            monkeypatch.setenv(name, "secret")
        monkeypatch.setenv("SYNAPSE_KNOWLEDGE_PARSE_TIMEOUT_S", "99")
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        env = child_env()
        assert not {"DATABASE_URL", "REDIS_URL", "MODEL_GATEWAY_SIGNING_KEY", "AWS_SECRET_ACCESS_KEY",
                    "OPENAI_API_KEY", "SYNAPSE_DB_URL", "SYNAPSE_INTERNAL_TOKEN"} & set(env)
        assert env["SYNAPSE_KNOWLEDGE_PARSE_TIMEOUT_S"] == "99" and env["LC_ALL"] == "C.UTF-8"

    async def test_the_child_process_sees_the_same(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://secret")
        async with run_parse(b"x", _job("kb/a.pdf", b"x", fake="env"), timeout_s=30, memory_mb=1024,
                             command=FAKE_CHILD) as out:
            names = set(out.parsed.text.split())
        assert "DATABASE_URL" not in names and "PYTHONPATH" in names


class TestTheHeartbeat:
    async def test_it_beats_while_the_block_runs_and_stops_after(self, monkeypatch):
        monkeypatch.setattr(limits, "HEARTBEAT_S", 0.05)
        beats = []

        async def beat():
            beats.append(1)

        async with service._beating(beat):
            await asyncio.sleep(0.3)
        seen = len(beats)
        await asyncio.sleep(0.2)
        assert seen >= 3 and len(beats) == seen

    async def test_a_failing_beat_never_stops_the_parse(self, monkeypatch):
        monkeypatch.setattr(limits, "HEARTBEAT_S", 0.02)

        async def beat():
            raise RuntimeError("database away")

        async with service._beating(beat):
            await asyncio.sleep(0.1)


@pytest.mark.usefixtures("knowledge_store")
class TestThroughTheJob:
    @pytest.fixture(autouse=True)
    async def _on(self, monkeypatch):
        dispatch.reset()
        use_saved_settings(monkeypatch)
        await enable_knowledge()
        self.indexer = FakeIndexer()
        install_fake_indexing(monkeypatch, self.indexer)
        yield
        dispatch.reset()

    async def _ingest(self, path: str, data: bytes):
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        write_vault(path, data)
        row, _ = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None)
        report = await run_ingest_job(IngestJob(tenant="default", kb_id=kb.id, op="upsert", reason="vault_write",
                                                path=path, doc_id=row.id))
        return report, await documents.get(row.id)

    async def test_an_encrypted_file_fails_with_its_reason_and_no_retry(self):
        report, row = await self._ingest("kb/secret.docx", OLE + "EncryptionInfo".encode("utf-16-le"))
        assert (row.status, row.status_reason) == ("failed", "encrypted")
        parse = report.stages[0]
        assert (parse.stage, parse.status, parse.error.code) == ("parse", "failed", "encrypted")
        assert not report.retry.allowed and "password" in report.retry.hint
        assert self.indexer.contexts == []

    async def test_a_parser_that_fails_with_nothing_read_fails_the_document(self, monkeypatch):
        async def failing(*a, **kw):
            raise ParserFailed("Parsing took longer than 1 s and was stopped.", reason="parser_timeout")

        monkeypatch.setattr(service, "parse_document", failing)
        report, row = await self._ingest("kb/big.pdf", b"%PDF-1.7")
        assert (row.status, row.status_reason) == ("failed", "parser_timeout") and report.retry.allowed

    async def test_a_big_text_file_parses_in_the_subprocess(self, monkeypatch):
        monkeypatch.setattr(limits, "IN_PROCESS_MAX_BYTES", 10)
        report, row = await self._ingest("kb/notes.md", b"# Notes\n\nA paragraph long enough.")
        assert row.status == "indexed" and report.stages[0].detail["runner"] == "subprocess"
        assert report.stages[0].detail["peak_rss_mb"] >= 0

    async def test_a_small_one_parses_in_a_thread(self):
        report, row = await self._ingest("kb/notes.md", b"# Notes\n\nShort.")
        assert row.status == "indexed" and report.stages[0].detail["runner"] == "thread"


@pytest.mark.usefixtures("knowledge_store")
class TestTheHeartbeatColumn:
    async def test_only_a_parsing_document_is_touched(self, monkeypatch):
        use_saved_settings(monkeypatch)
        await enable_knowledge()
        kb = await bases.create_base(KnowledgeBaseCreate(name="KB", folder="kb"))
        write_vault("kb/a.md", "a")
        row, _ = await documents.upsert_for_path(kb, "kb/a.md", source="vault", sha256=None, size=None)
        assert not await documents.heartbeat(row.id)  # queued: nothing to keep alive
        started = (await documents.transition(row.id, "parsing", started_at=None)).started_at
        assert await documents.heartbeat(row.id)
        beaten = await documents.get(row.id)
        assert beaten.started_at is not None and started is None and beaten.status == "parsing"
        await documents.tombstone(row.id)
        before = (await documents.get(row.id)).updated_at
        assert not await documents.heartbeat(row.id)
        assert (await documents.get(row.id)).updated_at == before  # a tombstone keeps its age
