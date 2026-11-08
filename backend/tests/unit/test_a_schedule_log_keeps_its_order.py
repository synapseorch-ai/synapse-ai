"""A schedule run's log keeps its lines in the order they happened.

`ScheduleLogger` handed each line to the default thread pool, which has many
workers, so a slow write could land after a later one: SP7 saw "AGENT RESPONSE"
after the "SCHEDULE RUN FINISHED" banner. And `close()` wrote an empty string
on its own thread as a "barrier" that waited for nothing, so the log uploaded
to the blob store could be missing its last lines. The orchestration logger
already writes through the journal's one writer thread; this one does now too.
"""
import time

from core.schedule_logger import ScheduleLogger


class TestTheOrder:
    async def test_the_answer_lands_before_the_banner_and_the_upload_has_both(self, monkeypatch):
        real = ScheduleLogger._write

        def slow_answer(self, text):
            if "AGENT RESPONSE" in text:
                time.sleep(0.2)
            real(self, text)

        monkeypatch.setattr(ScheduleLogger, "_write", slow_answer)
        log = ScheduleLogger("sched_order", "Order", "agent", "agent_x", "go")
        log.log_event({"type": "tool_execution", "tool_name": "lookup", "args": {}})
        log.log_event({"type": "final", "response": "the answer"})
        log.run_end("completed")
        log.close()

        from core.storage import get_blob_store
        uploaded = get_blob_store().get(f"logs/schedule/{log.run_id}.log")
        assert uploaded, "the log was uploaded"
        assert "AGENT RESPONSE" in uploaded, "the upload is missing a line written before close()"
        assert uploaded.index("TOOL CALL") < uploaded.index("AGENT RESPONSE") < uploaded.index("SCHEDULE RUN FINISHED")
