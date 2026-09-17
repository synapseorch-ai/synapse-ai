"""
A human step's question, posted to the messaging channel the step names.

A human step with `human_channel_id` asks its question in that channel as well
as in the app, and a reply there answers it. Until F54 neither half happened:
the step armed an in-memory Future and discarded the task awaiting it, the
"send the prompt" block only logged, and a reply resolved a Future whose result
went nowhere — the adapter said "Resuming the workflow…" and the run stayed
paused. A Future also could not have worked on a fleet (the worker running the
step is not the process receiving the reply) or across a restart.

So the channel lives with the run instead: the engine records it in the run's
state when the step pauses (`HUMAN_CHANNEL_KEY`, checkpointed like
`_model_override`), and a reply looks for the newest run waiting on that
channel (`core/messaging/manager.py::find_run_waiting_on`) and answers it the
way the browser does (`core/orchestration/answers.py`).

**Sending is a seam, not a dependency.** The engine calls whatever sender a
process installed with `set_human_prompt_sender`. The shipped product installs
one in the API server and in its own worker startup; an embedder that delivers
channel messages its own way installs none, and nothing is sent twice.

Called from `OrchestrationEngine._execute_loop` at the moment a human step
pauses — the one place an in-process run, an in-process resume, a fleet run and
a fleet resume all pass through — and never allowed to fail the run.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from core.log import get_logger

_log = get_logger(__name__)

#: Where the engine records the channel a paused run is waiting on.
HUMAN_CHANNEL_KEY = "_human_channel_id"

#: One bounded wait. The run is already checkpointed as paused when this runs;
#: a platform that stops answering must not hold the pause event back for long.
SEND_TIMEOUT_SECONDS = 15

PromptSender = Callable[..., Awaitable[None]]

_sender: Optional[PromptSender] = None


def set_human_prompt_sender(sender: Optional[PromptSender]) -> None:
    """Install the process's sender: `await sender(channel_id=, prompt=, run_id=)`.
    None removes it."""
    global _sender
    _sender = sender


async def announce(run_id: str, channel_id: str, prompt: str) -> None:
    """Post a paused human step's question to its channel, if a sender is installed."""
    if _sender is None or not channel_id:
        return
    try:
        await asyncio.wait_for(
            _sender(channel_id=channel_id, prompt=prompt, run_id=run_id),
            SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — a failed send never fails the run
        _log.warning(f"human step prompt for run {run_id} not sent to channel {channel_id}: {exc}")
