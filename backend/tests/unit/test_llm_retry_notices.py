"""
A provider retry is told to whoever is watching, and never promised falsely.

Every retry loop in `core/llm_providers.py` logged "Retrying in Ns" at `debug`
— invisible at a worker's INFO level — and nothing else. So a call that timed
out and was retried was a stretch of silence on the run page, and on the last
attempt the line promised a retry that never came. Driven through the
gateway-shaped `call_v1_compatible`, which is the path the cloud's managed
models take.
"""
import asyncio

import httpx
import pytest

import core.llm_providers as providers
import core.model_wait as mw

_OK = {
    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


@pytest.fixture
def instant_backoff(monkeypatch):
    real_sleep = asyncio.sleep

    async def no_wait(_delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(providers.asyncio, "sleep", no_wait)


def _transport(monkeypatch, outcomes):
    """Each request takes the next outcome: an exception to raise, or a response."""
    seen = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        outcome = outcomes[min(len(seen), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        providers.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


async def _call_watched():
    wait = mw.ModelWait(
        providers.call_v1_compatible(
            "synapse.test", [{"role": "user", "content": "hi"}], "", "http://gateway:8080", "tok"
        ),
        label="Agent step",
    )
    events = [event async for event in wait.events()]
    return wait, [e["message"] for e in events if e["type"] == "status"]


async def test_a_timed_out_attempt_is_retried_out_loud(monkeypatch, instant_backoff):
    seen = _transport(monkeypatch, [httpx.ReadTimeout("slow"), httpx.Response(200, json=_OK)])

    wait, notices = await _call_watched()

    assert len(seen) == 2
    assert wait.result.text == "answer"
    assert len(notices) == 1
    assert "did not answer" in notices[0] and "retrying (2 of 5)" in notices[0]


async def test_a_refused_attempt_names_the_status(monkeypatch, instant_backoff):
    _transport(monkeypatch, [httpx.Response(503, text="busy"), httpx.Response(200, json=_OK)])

    _, notices = await _call_watched()

    assert len(notices) == 1 and "503" in notices[0]


async def test_the_last_attempt_does_not_promise_a_retry(monkeypatch, instant_backoff):
    _transport(monkeypatch, [httpx.ReadTimeout("slow")])
    told = []
    monkeypatch.setattr(mw, "report", told.append)

    with pytest.raises(providers.LLMError):
        await providers.call_v1_compatible(
            "synapse.test", [{"role": "user", "content": "hi"}], "", "http://gateway:8080", "tok"
        )

    # Five attempts, four retries. The fifth failure is the end, not a promise.
    assert len(told) == 4, told
    assert [m.split("retrying (")[1].split(")")[0] for m in told] == [
        "2 of 5", "3 of 5", "4 of 5", "5 of 5"
    ]
