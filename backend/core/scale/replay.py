"""
Queueing a replay of a failed or cancelled run onto the fleet.

Two routes replay a run a worker ran — Replay on the run (`routes/orchestrations.py`)
and Retry on a dead letter (`routes/scale.py`) — and both queue
`resume_failed_job`, which resumes from the run's checkpoint.

**A cancel's stop signal must not stop the replay.** Cancel leaves
`synapse:cancel:{run}` for an hour so that a resume an answer queued just before
it dies on arrival, and a job that runs clears it when it ends. A run cancelled
while nothing was executing it has no such job, so the key outlives the run:
the replay's adapter found it and finished `cancelled` at once. Replay is a
person asking for the run to go again, so the key is withdrawn — unless the
resume it exists to stop is still on the queue. Cloud's Replay learned this in
its browser pass (2026-09-16); this is the same rule.
"""
import time

from core.scale.config import QUEUE_NAME
from core.scale.pubsub import clear_cancellation


async def queue_replay(redis_client, arq_redis, run_id: str):
    """Queue `resume_failed_job` for `run_id`. Returns arq's job, or None when a
    job with that id is already held."""
    resume_queued = False
    async for _member in redis_client.zscan_iter(QUEUE_NAME, match=f"resume_{run_id}_*"):
        resume_queued = True
        break
    if not resume_queued:
        await clear_cancellation(redis_client, run_id)

    return await arq_redis.enqueue_job(
        "resume_failed_job",
        run_id=run_id,
        _queue_name=QUEUE_NAME,
        _job_id=f"retry_{run_id}_{int(time.time() * 1000)}",
    )
