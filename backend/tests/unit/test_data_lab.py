"""
Data Lab generates through the engine's own model call, says when it failed,
and never puts a key in a URL (F210).

It posted to Google itself with a hard-coded `gemini-1.5-flash` — retired, so
every record 404'd — and the key the browser sent rode in the query string, so
each failed record printed the full key into the backend log. The job then
ended `completed` with 0 of 3 records and `error: null`, and tried to publish a
file that was never written. Measured by the OSS test pass.

Now: the model is the one the request names, else the workspace default, and
the engine's providers hold the key (no key comes from the browser); a job with
no record is `failed` with the first reason, a partial one says how many
failed; nothing is published when nothing was written. The model list's Gemini
call sends its key as a header too.
"""
import json

import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from services import synthetic_data as dl

PAIR = json.dumps({"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]})
KEY = "AIza" + "k" * 35


@pytest.fixture(autouse=True)
def _idle_job():
    # In place: the route module holds a reference to this very dict.
    dl.current_job.update(status="idle", completed=0, total=0, file=None, error=None)
    yield
    dl.current_job.update(status="idle", completed=0, total=0, file=None, error=None)


@pytest.fixture
def model_calls(monkeypatch):
    """The engine's model call, scripted per call: a string answers, an
    exception fails that record."""
    import core.llm_providers as llm

    calls, script = [], []

    async def generate_response(**kwargs):
        calls.append(kwargs)
        outcome = script.pop(0) if script else PAIR
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm, "generate_response", generate_response)
    return calls, script


def _published():
    from core.storage import get_blob_store
    return [k for k in get_blob_store().list(dl.DATASETS_PREFIX) if k.endswith(".jsonl")]


async def test_records_come_from_the_workspace_model_when_none_is_named(model_calls, monkeypatch):
    import core.config as config

    calls, _ = model_calls
    monkeypatch.setattr(config, "load_settings", lambda: {"model": "oaic.slow1"})

    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic="Tides", count=3))

    assert dl.current_job["status"] == "completed"
    assert dl.current_job["completed"] == 3 and dl.current_job["error"] is None
    assert [c["current_model"] for c in calls] == ["oaic.slow1"] * 3
    assert len(_published()) == 1


async def test_a_named_model_is_the_one_called(model_calls):
    calls, _ = model_calls

    await dl.generate_synthetic_data(
        dl.SyntheticDataRequest(topic="Tides", count=1, model="gemini-2.5-flash")
    )

    assert calls[0]["current_model"] == "gemini-2.5-flash"


async def test_a_job_with_no_record_is_failed_with_the_reason(model_calls):
    _, script = model_calls
    script.extend([RuntimeError("404 model retired")] * 3)

    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic="Tides", count=3))

    assert dl.current_job["status"] == "failed"
    assert dl.current_job["completed"] == 0
    assert "404 model retired" in dl.current_job["error"]
    assert _published() == []


async def test_a_partial_job_says_how_many_failed(model_calls):
    _, script = model_calls
    script.extend([PAIR, "not json at all", PAIR])

    await dl.generate_synthetic_data(dl.SyntheticDataRequest(topic="Tides", count=3))

    assert dl.current_job["status"] == "completed"
    assert dl.current_job["completed"] == 2
    assert dl.current_job["error"].startswith("1 of 3 failed")


async def test_the_browser_no_longer_sends_a_key():
    assert "api_key" not in dl.SyntheticDataRequest.model_fields


async def test_the_gemini_model_list_sends_its_key_as_a_header(client, monkeypatch):
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {"gemini_key": KEY})
    seen = []

    def handler(url, kwargs):
        if "generativelanguage.googleapis.com" in url:
            seen.append((url, kwargs.get("headers") or {}))
            return FakeResponse({"models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            ]})
        return FakeResponse({}, status_code=404)

    install_httpx(monkeypatch, handler)
    gemini = (await client.get("/api/models")).json()["providers"]["gemini"]

    assert "gemini-2.5-flash" in gemini["models"]
    url, headers = seen[0]
    assert KEY not in url
    assert headers.get("x-goog-api-key") == KEY
