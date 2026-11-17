"""A small local model is left out of Synapse AI's picker, and its Ollama tip names gpt-oss:20b (F1598).

Synapse AI's pickers leave out a model not for building: a dense model of 9B or less. The facts knew such models by
their cloud providers' spellings only, so Ollama's (`llama3.1:8b`, `gemma3:4b`, `qwen3:8b`, `phi4-mini`, `mistral:7b`)
were unknown and offered for building. Each common small local model now has a row in Ollama's own spelling, from
ollama.com's tags pages, with and without its `:latest` tag (Ollama's name alone is its latest), and is left out by the
same rule. The first-run step's Ollama tip and Synapse AI's refusal named llama3.1 (8B): they now name gpt-oss:20b, a
mixture of experts (21B, 3.6B active) that reads 128K, about a 14 GB download that runs in 16 GB of memory.

Near misses: gpt-oss:20b is small by the rule but not a dense model of 9B or less, so it is shown, unmarked; so are a
dense local model of 10B or more (gemma3:12b) and qwen3:30b; a local model the facts do not know keeps today's
behaviour (no mark); llama3.1's 70B is never left out.
"""
from __future__ import annotations

import pytest

from core.native_builder import models_menu as MM
from core.providers import model_facts as F

LEFT_OUT = [
    "ollama.llama3.1:8b", "ollama.llama3.1:latest", "ollama.llama3.1",
    "ollama.llama3.2:3b", "ollama.llama3.2:1b", "ollama.llama3.2",
    "ollama.gemma3:4b", "ollama.gemma3:1b", "ollama.gemma3:270m", "ollama.gemma3",
    "ollama.qwen3:8b", "ollama.qwen3:4b", "ollama.qwen3:1.7b", "ollama.qwen3:0.6b", "ollama.qwen3",
    "ollama.phi4-mini:3.8b", "ollama.phi4-mini",
    "ollama.mistral:7b", "ollama.mistral",
]


@pytest.mark.parametrize("model", LEFT_OUT)
def test_a_small_local_model_is_not_for_building(model):
    assert F.builder_fit(model, window=131_072, tools="native") == "not_for_building", F.size_of(model)


@pytest.mark.parametrize("model, total, active", [
    ("ollama.gpt-oss:20b", 21.0, 3.6), ("ollama.gpt-oss", 21.0, 3.6),
    ("ollama.gemma3:12b", 12.0, None), ("ollama.qwen3:30b", 30.5, 3.3),
])
def test_the_ones_shown_instead_have_their_facts_and_no_mark(model, total, active):
    row = F.size_of(model)
    assert row is not None and (row.total, row.active) == (total, active) and row.fit is None, row
    assert F.builder_fit(model, window=131_072, tools="native") is None


def test_a_local_model_the_facts_do_not_know_keeps_todays_behaviour():
    assert F.size_of("ollama.my-own-model:latest") is None
    assert F.builder_fit("ollama.my-own-model:latest", window=131_072, tools="native") is None


def test_llama3_1s_70b_is_never_left_out():
    assert F.builder_fit("ollama.llama3.1:70b", window=131_072, tools="native") is None


def test_the_tip_names_gpt_oss_20b():
    assert MM.SYNAPSE_AI_LOCAL_MODEL == "gpt-oss:20b"
