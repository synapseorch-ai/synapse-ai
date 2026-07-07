"""
Stress-suite configuration.

Activates the realistic-latency fake-LLM profile (random 5-90s on a fraction of
calls) for every test in this directory. All knobs are env-overridable so a
quick local run can bound the numbers, e.g.:

    SYNAPSE_FAKE_LLM_DELAY_MAX=2 SYNAPSE_STRESS_TOTAL=8 \\
        python -m pytest backend/tests/stress -m stress -q
"""
import os

import pytest


@pytest.fixture(autouse=True)
def stress_delay_profile(monkeypatch):
    """Default to the 5-90s 'sometimes slow' profile; honor pre-set env values."""
    monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MIN", os.getenv("SYNAPSE_FAKE_LLM_DELAY_MIN", "5"))
    monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MAX", os.getenv("SYNAPSE_FAKE_LLM_DELAY_MAX", "90"))
    monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_PROB", os.getenv("SYNAPSE_FAKE_LLM_DELAY_PROB", "0.3"))
    yield


@pytest.fixture
def stress_params():
    return {
        "total": int(os.getenv("SYNAPSE_STRESS_TOTAL", "24")),
        "concurrency": int(os.getenv("SYNAPSE_STRESS_CONCURRENCY", "12")),
    }
