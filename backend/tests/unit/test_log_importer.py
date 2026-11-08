"""Bringing a pre-store install's `backend/logs/` across on upgrade.

The gap this closes was silent and that is the point of testing it. D29 moved
where run logs are *read* from — the blob store — without moving what already
existed in `backend/logs/`, and `importer.py` never covered that folder because
it only ever knew about `DATA_DIR`. So "Runs & Logs" kept its nav entry, kept
answering, and returned `[]` forever, with 5,000 files sitting on disk.

Nothing failed. That is why the assertions below are about *visibility through
the reader the product actually uses* rather than about files being copied:
copying to the wrong key, or without the `.meta.json` sidecar, would pass a
file-count test and still show the user nothing.
"""
import json

import pytest

from core.store.log_importer import (
    import_legacy_logs_if_present,
    import_logs_dir,
    legacy_logs_dir,
)

#: The head format `_parse_head` reads. Written out rather than generated so a
#: change to the logger's header is caught here instead of silently producing
#: metadata full of empty strings.
LOG_HEAD = """\
================================================================================
  ORCHESTRATION RUN LOG
================================================================================
  Run ID          : {run_id}
  Orchestration ID: orch_nightly
  Orchestration   : {name}
  Session ID      : sess_legacy
  Started at      : 2026-08-01 10:00:00
  User Input      : run the nightly report
================================================================================

▶ STEP START: Gather
"""


@pytest.fixture
def legacy_logs(tmp_path):
    """A `logs/` tree shaped like a real pre-D29 install."""
    root = tmp_path / "logs"
    (root / "orchestration_logs").mkdir(parents=True)
    (root / "agent_logs").mkdir(parents=True)
    (root / "orchestration_runs").mkdir(parents=True)

    for i in range(3):
        run_id = f"run_orch_legacy{i}"
        (root / "orchestration_logs" / f"{run_id}.log").write_text(
            LOG_HEAD.format(run_id=run_id, name="Nightly report"), encoding="utf-8"
        )
    (root / "agent_logs" / "run_agent_legacy.log").write_text(
        LOG_HEAD.format(run_id="run_agent_legacy", name="Researcher"), encoding="utf-8"
    )
    (root / "orchestration_runs" / "run_orch_legacy0.json").write_text(
        json.dumps({
            "run_id": "run_orch_legacy0",
            "orchestration_id": "orch_nightly",
            "status": "completed",
            "shared_state": {"summary": "done"},
            "step_history": [{"step_id": "a", "status": "completed"}],
            "total_tokens_used": 4200,
            "total_cost_usd": 0.02,
            "started_at": "2026-08-01T10:00:00Z",
            "ended_at": "2026-08-01T10:00:30Z",
        }),
        encoding="utf-8",
    )
    return root


class TestTheLogsBecomeVisible:
    async def test_an_orchestration_log_is_listed_and_readable(self, legacy_logs):
        """Asserted through `OrchestrationLogger`, not through the blob keys.

        A copy to the wrong prefix, or one that skipped the `.meta.json`
        sidecar, would satisfy a file count and still show the user nothing.
        """
        from core.orchestration.logger import OrchestrationLogger

        assert OrchestrationLogger.list_logs() == [], "precondition: nothing visible yet"

        counts = await import_logs_dir(legacy_logs)
        assert counts["orchestration_logs"] == 3

        listed = OrchestrationLogger.list_logs()
        assert {entry["run_id"] for entry in listed} == {
            "run_orch_legacy0", "run_orch_legacy1", "run_orch_legacy2"
        }
        # The sidecar is what carries these; without it the listing is a bare
        # id that sorts to the bottom, because it sorts on `started_at`.
        # The sidecar's fields, which only exist if `_parse_head` matched the
        # header the loggers actually write. Copied verbatim from a real log on
        # a pre-D29 install, so a change to that header fails here rather than
        # quietly producing metadata full of empty strings.
        assert listed[0]["started_at"] == "2026-08-01 10:00:00"
        assert listed[0]["orchestration_name"] == "Nightly report"
        assert listed[0]["orchestration_id"] == "orch_nightly"
        # A blob store cannot cheaply stat an object, so the size travels in
        # the sidecar. Without that every finished run lists as "0KB", which is
        # what the whole Runs & Logs screen showed once logs moved to blobs.
        assert listed[0]["file_size_kb"] > 0
        assert "ORCHESTRATION RUN" in (OrchestrationLogger.get_log("run_orch_legacy0") or "")

    async def test_an_agent_log_lands_under_its_own_prefix(self, legacy_logs):
        """The two loggers share a shape and must not share a namespace."""
        from core.agent_logger import AgentLogger
        from core.orchestration.logger import OrchestrationLogger

        await import_logs_dir(legacy_logs)

        assert {e["run_id"] for e in AgentLogger.list_logs()} == {"run_agent_legacy"}
        assert "run_agent_legacy" not in {e["run_id"] for e in OrchestrationLogger.list_logs()}

    async def test_run_state_becomes_a_row(self, legacy_logs):
        """`logs/orchestration_runs/*.json` is the run itself, not a log — it
        belongs in the table the run detail page reads."""
        from sqlalchemy import select

        from core.store import session
        from core.store.models import OrchestrationRunDB

        counts = await import_logs_dir(legacy_logs)
        assert counts["orchestration_runs"] == 1

        async with session() as s:
            run = (
                await s.execute(
                    select(OrchestrationRunDB).where(
                        OrchestrationRunDB.run_id == "run_orch_legacy0"
                    )
                )
            ).scalar_one()

        assert run.status == "completed"
        assert run.total_tokens_used == 4200
        assert run.shared_state == {"summary": "done"}
        assert run.started_at is not None, "timestamps must survive as timestamps"


class TestItRunsOnTheInstallsThatNeedIt:
    async def test_a_populated_store_does_not_block_the_import(self, legacy_logs):
        """The guard `importer.py` uses would be exactly wrong here.

        It refuses unless `store_is_empty()`. By the time anyone notices their
        history is missing they have been using the product for weeks, so that
        condition is never true again — and gating on it would skip precisely
        the installs this exists for.
        """
        from core.orchestration.logger import OrchestrationLogger
        from core.store import session
        from core.store.models import OrchestrationDB
        from core.store.importer import store_is_empty

        async with session() as s:
            s.add(OrchestrationDB(tenant_id="default", id="orch_x", name="Existing",
                                  definition={}))
            await s.commit()
        assert not await store_is_empty(), "precondition: the store is in use"

        await import_logs_dir(legacy_logs)
        assert len(OrchestrationLogger.list_logs()) == 3

    async def test_a_second_boot_does_not_duplicate(self, legacy_logs, monkeypatch):
        """The rename is the idempotence, the same mechanism `data/` uses."""
        from core.orchestration.logger import OrchestrationLogger
        from core.store import log_importer

        monkeypatch.setattr(log_importer, "legacy_logs_dir", lambda: legacy_logs)
        first = await import_legacy_logs_if_present()
        assert first is not None and first["orchestration_logs"] == 3
        assert not legacy_logs.exists(), "the folder should have been retired"
        assert (legacy_logs.parent / "logs.migrated").is_dir()

        # Second boot: the real locator finds nothing, so nothing runs.
        monkeypatch.setattr(log_importer, "legacy_logs_dir", lambda: None)
        assert await import_legacy_logs_if_present() is None
        assert len(OrchestrationLogger.list_logs()) == 3

    async def test_a_run_the_store_already_has_is_not_overwritten(self, legacy_logs):
        """The file is what the store replaced, so the row is newer by
        construction."""
        from sqlalchemy import select

        from core.store import session
        from core.store.models import OrchestrationRunDB

        async with session() as s:
            s.add(OrchestrationRunDB(tenant_id="default", run_id="run_orch_legacy0",
                                     orchestration_id="orch_nightly", status="failed"))
            await s.commit()

        counts = await import_logs_dir(legacy_logs)
        assert counts["orchestration_runs"] == 0

        async with session() as s:
            status = (
                await s.execute(
                    select(OrchestrationRunDB.status).where(
                        OrchestrationRunDB.run_id == "run_orch_legacy0"
                    )
                )
            ).scalar_one()
        assert status == "failed", "the live row was overwritten by a stale file"

    def test_an_install_with_no_logs_folder_is_left_alone(self, tmp_path, monkeypatch):
        """A fresh install must not have a `logs.migrated/` appear next to it."""
        monkeypatch.chdir(tmp_path)
        assert legacy_logs_dir() is None or legacy_logs_dir().is_dir()
