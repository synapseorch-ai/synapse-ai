"""Regression tests for #352: Synapse must read/write its own files as UTF-8,
never the platform default (cp1252 on Windows).

The reported crash was an orchestration checkpoint write:
    UnicodeEncodeError: 'charmap' codec can't encode characters ...
      state.py, in checkpoint: f.write(self.run.model_dump_json(indent=2))
because os.fdopen(fd, "w") used the platform-default encoding. These tests
assert non-ASCII content round-trips and is persisted as UTF-8 bytes.
"""
import json

NON_ASCII = "café résumé — €uro ✓ Société 日本語"


class TestOrchestrationCheckpoint:
    """Non-ASCII run state survives a checkpoint.

    This began as a regression test for a UnicodeEncodeError: the checkpoint
    was a file opened without an explicit encoding, so `model_dump_json`'s raw
    non-ASCII output crashed on a cp1252 platform. Run state lives in the store
    now, so there is no file and no platform-default encoding to get wrong —
    but the property the test was protecting is the same one, and it is still
    worth holding: what goes into a run comes back out of it unchanged.
    """

    async def test_checkpoint_roundtrips_non_ascii(self):
        from core.orchestration import state as state_mod
        from core.models_orchestration import OrchestrationRun

        run = OrchestrationRun(run_id="utf8-run", orchestration_id="o1",
                               shared_state={"contrat": NON_ASCII})
        await state_mod.SharedState(run).checkpoint()

        restored = await state_mod.SharedState.restore("utf8-run")
        assert restored.run.shared_state["contrat"] == NON_ASCII

    async def test_list_runs_reads_non_ascii(self):
        from core.orchestration import state as state_mod
        from core.models_orchestration import OrchestrationRun

        run = OrchestrationRun(run_id="r1", orchestration_id=NON_ASCII)
        await state_mod.SharedState(run).checkpoint()

        runs = await state_mod.SharedState.list_runs()
        assert any(r["orchestration_id"] == NON_ASCII for r in runs)

