"""
The per-run event journal (core.orchestration.journal).

Contracts under test:
- append/read round-trip with monotonic ids and `after_id` filtering
- sequence recovery across reopen (a resume pump continues the same file)
- torn final line (crash mid-write) costs at most that one event
- oversized events are shrunk, never dropped
- the event cap degrades to structural-events-only, never unbounded growth
- broker: subscribe-before-replay dedupe (the replay→live race)
"""
import json

import asyncio

from core.orchestration.journal import (
    MAX_EVENT_BYTES,
    MAX_EVENTS_PER_RUN,
    FileRunJournal,
    RunEventBroker,
    close_journal,
    get_journal,
)


class TestAppendRead:
    def test_round_trip_with_after_id(self):
        j = FileRunJournal("run_j1")
        ids = [j.append({"type": "step_start", "n": n}) for n in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert j.last_id() == 5

        entries = j.read()
        assert [e["id"] for e in entries] == [1, 2, 3, 4, 5]
        assert entries[2]["event"] == {"type": "step_start", "n": 2}
        assert all("ts" in e for e in entries)

        assert [e["id"] for e in j.read(after_id=3)] == [4, 5]
        assert j.read(after_id=5) == []
        j.close()

    def test_read_limit(self):
        j = FileRunJournal("run_j_limit")
        for n in range(10):
            j.append({"type": "thinking", "n": n})
        assert [e["id"] for e in j.read(limit=3)] == [1, 2, 3]
        j.close()

    def test_read_missing_file_is_empty(self):
        assert FileRunJournal("run_never_written").read() == []

    def test_exists_and_delete(self):
        j = FileRunJournal("run_j_del")
        j.append({"type": "step_start"})
        j.close()
        assert FileRunJournal.exists("run_j_del")
        assert FileRunJournal.delete("run_j_del")
        assert not FileRunJournal.exists("run_j_del")
        assert not FileRunJournal.delete("run_j_del")


class TestSequenceRecovery:
    def test_reopen_continues_ids(self):
        """run pump → close → resume pump must extend, not restart, the ids."""
        j1 = FileRunJournal("run_seq")
        j1.append({"type": "step_start"})
        j1.append({"type": "human_input_required"})
        j1.close()

        j2 = FileRunJournal("run_seq")
        assert j2.last_id() == 2
        assert j2.append({"type": "step_complete"}) == 3
        assert [e["id"] for e in j2.read()] == [1, 2, 3]
        j2.close()

    def test_torn_final_line_skipped_and_seq_recovers(self):
        j = FileRunJournal("run_torn")
        j.append({"type": "step_start"})
        j.append({"type": "step_complete"})
        j.close()
        # Simulate a crash mid-write: a partial JSON line at EOF.
        with open(j.path, "a", encoding="utf-8") as f:
            f.write('{"id": 3, "ts": 1.0, "event": {"type": "trunc')

        j2 = FileRunJournal("run_torn")
        assert j2.last_id() == 2  # torn line ignored for recovery
        assert [e["id"] for e in j2.read()] == [1, 2]  # and for replay
        assert j2.append({"type": "step_error"}) == 3
        j2.close()

    def test_registry_shares_instance_and_close_evicts(self):
        a = get_journal("run_reg")
        b = get_journal("run_reg")
        assert a is b
        a.append({"type": "step_start"})
        close_journal("run_reg")
        c = get_journal("run_reg")
        assert c is not a
        assert c.last_id() == 1
        close_journal("run_reg")


class TestShrink:
    def test_oversized_event_truncated_not_dropped(self):
        j = FileRunJournal("run_big")
        big = {"type": "final", "response": "x" * (MAX_EVENT_BYTES * 2)}
        assert j.append(big) == 1
        [entry] = j.read()
        assert entry["event"]["type"] == "final"
        assert entry["event"]["truncated"] is True
        assert len(entry["event"]["response"]) < 3000
        # The journaled line itself is bounded.
        assert len(json.dumps(entry)) < MAX_EVENT_BYTES
        j.close()

    def test_small_event_untouched(self):
        j = FileRunJournal("run_small")
        j.append({"type": "tool_result", "preview": "ok"})
        [entry] = j.read()
        assert "truncated" not in entry["event"]
        j.close()

    def test_nested_heavy_fields_shrunk(self):
        j = FileRunJournal("run_nested")
        event = {"type": "orchestration_complete",
                 "final_state": {"doc": "y" * (MAX_EVENT_BYTES * 2)}}
        j.append(event)
        [entry] = j.read()
        assert entry["event"]["truncated"] is True
        assert len(entry["event"]["final_state"]["doc"]) < 3000
        j.close()


class TestEventCap:
    def test_cap_switches_to_structural_only(self, monkeypatch):
        import core.orchestration.journal as journal_mod
        monkeypatch.setattr(journal_mod, "MAX_EVENTS_PER_RUN", 5)

        j = FileRunJournal("run_cap")
        for n in range(5):
            assert j.append({"type": "thinking", "n": n}) > 0
        # Cap hit: chatty events are skipped (id 0), structural still recorded.
        assert j.append({"type": "thinking", "n": 99}) == 0
        assert j.append({"type": "step_complete"}) > 0

        types = [e["event"]["type"] for e in j.read()]
        assert "journal_truncated" in types
        assert types[-1] == "step_complete"
        assert {"type": "thinking", "n": 99} not in [e["event"] for e in j.read()]
        j.close()


class TestBroker:
    async def test_publish_reaches_all_subscribers(self):
        broker = RunEventBroker()
        q1, q2 = broker.subscribe("r1"), broker.subscribe("r1")
        other = broker.subscribe("r2")
        broker.publish("r1", {"id": 1, "event": {"type": "step_start"}})
        assert (await q1.get())["id"] == 1
        assert (await q2.get())["id"] == 1
        assert other.empty()

    async def test_unsubscribe_stops_delivery(self):
        broker = RunEventBroker()
        q = broker.subscribe("r1")
        broker.unsubscribe("r1", q)
        broker.publish("r1", {"id": 1, "event": {}})
        assert q.empty()
        # Unsubscribing an unknown queue is a no-op.
        broker.unsubscribe("r1", asyncio.Queue())
        broker.unsubscribe("r_unknown", q)

    async def test_replay_then_live_dedupe(self):
        """The A4 handoff: subscribe first, replay the file, dedupe overlap.

        An event appended (and published) *during* replay is delivered twice —
        once from the file, once from the queue — and the monotonic-id check
        must drop the queued duplicate.
        """
        broker = RunEventBroker()
        j = FileRunJournal("run_race")
        for n in range(3):
            eid = j.append({"type": "step_start", "n": n})
            broker.publish("run_race", {"id": eid, "event": {"type": "step_start", "n": n}})

        q = broker.subscribe("run_race")  # subscribe BEFORE replay
        # Concurrent append lands after subscription but before replay reads.
        eid = j.append({"type": "step_complete", "n": 3})
        broker.publish("run_race", {"id": eid, "event": {"type": "step_complete", "n": 3}})

        seen = []
        last = 0
        for entry in j.read(after_id=last):  # replay sees ids 1..4
            last = entry["id"]
            seen.append(entry["id"])
        while not q.empty():  # live queue holds only id 4 — a duplicate
            entry = q.get_nowait()
            if entry["id"] <= last:
                continue
            last = entry["id"]
            seen.append(entry["id"])
        assert seen == [1, 2, 3, 4]
        j.close()


class TestOffLoopWriter:
    """`append` assigns the id on the loop; the write happens on one writer
    thread. Ids stay synchronous and monotonic, lines land in order, and a
    reader that wants everything so far drains first."""

    async def test_ids_are_synchronous_and_lines_land_in_order(self):
        j = FileRunJournal("run_order")
        ids = [j.append({"type": "thinking", "n": n}) for n in range(500)]
        assert ids == list(range(1, 501))
        await j.drain()
        seen = [e["event"]["n"] for e in FileRunJournal("run_order").read(limit=1000)]
        assert seen == list(range(500))
        j.close()

    async def test_the_file_write_is_not_on_the_event_loop_thread(self, monkeypatch):
        import threading
        threads: set[str] = set()
        real = FileRunJournal._write_line

        def spy(self, line):
            threads.add(threading.current_thread().name)
            return real(self, line)

        monkeypatch.setattr(FileRunJournal, "_write_line", spy)
        j = FileRunJournal("run_thread")
        j.append({"type": "step_start"})
        await j.drain()
        j.close()
        assert threads and threading.current_thread().name not in threads
        assert all(t.startswith("synapse-journal") for t in threads)

    async def test_drain_makes_a_throwaway_reader_complete(self):
        j = FileRunJournal("run_drain")
        for n in range(3):
            j.append({"type": "step_start", "n": n})
        await j.drain()
        assert [e["id"] for e in FileRunJournal("run_drain").read()] == [1, 2, 3]
        j.close()

    async def test_aclose_journal_drains_and_evicts(self):
        from core.orchestration.journal import aclose_journal, open_journal
        j = get_journal("run_aclose")
        j.append({"type": "step_start"})
        j.append({"type": "orchestration_complete"})
        assert open_journal("run_aclose") is j
        await aclose_journal("run_aclose")
        assert open_journal("run_aclose") is None
        assert [e["id"] for e in FileRunJournal("run_aclose").read()] == [1, 2]

    async def test_a_large_event_costs_the_loop_one_serialisation_not_the_disk(self):
        import time as _time
        j = FileRunJournal("run_big")
        big = {"type": "final", "response": "x" * 200_000}
        t0 = _time.perf_counter()
        j.append(big)
        on_loop_ms = (_time.perf_counter() - t0) * 1000
        await j.drain()
        j.close()
        # One json.dumps of 200 KB plus a shrink; no write, no flush.
        assert on_loop_ms < 25, on_loop_ms
        entry = FileRunJournal("run_big").read()[0]
        assert entry["event"]["truncated"] is True

    async def test_the_event_is_serialised_before_append_returns(self):
        """The terminal events reference the run's live state; a later
        mutation must not reach the journal (and cannot race the writer)."""
        j = FileRunJournal("run_mut")
        state = {"a": 1}
        j.append({"type": "orchestration_complete", "final_state": state})
        state["a"] = 2
        await j.drain()
        j.close()
        assert FileRunJournal("run_mut").read()[0]["event"]["final_state"] == {"a": 1}

    async def test_sync_close_waits_for_the_writer(self):
        j = FileRunJournal("run_syncclose")
        for n in range(50):
            j.append({"type": "thinking", "n": n})
        j.close()   # no drain first: close itself must wait
        assert len(FileRunJournal("run_syncclose").read()) == 50
