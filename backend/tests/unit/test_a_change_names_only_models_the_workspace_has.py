"""
A change names a model this workspace can call, and the change planner is shown which ones those are (SAI2-P2, D308).

Measured in P2's after-batch (scratch, Haiku 4.5 and Gemini 3 Flash): "make the Front desk agent use nova pro" had the
planner say the agent runs gpt-4.1-mini, ask which model "nova pro" is, and offer models from its own memory
(claude-3-5-sonnet-20241022, gemini-2.0-flash); the old builder had written `synapse.nova-pro`, a cloud model this
install cannot call, and an agent set to it answers nothing. The `models` topic now lists the models this workspace
offers, by provider (what the agent editor's picker offers: `/api/models`' `all_available`, or a host's own list),
and `check_change` refuses a model none of them is, in plain words, before the review.
"""
from __future__ import annotations

import json

import pytest

from core import model_choices

from test_the_change_route_decides_in_code import _agent, _call, _check, _run, fresh, role  # noqa: F401


@pytest.fixture(autouse=True)
def offered():
    async def choices():
        return {"anthropic": ["claude-haiku-4-5", "claude-sonnet-5-5"], "gemini": ["gemini-3-flash-preview"]}

    model_choices.set_model_choices(choices)
    yield
    model_choices.set_model_choices(None)


async def test_the_models_topic_lists_what_the_workspace_offers(client):
    got, _ = await _call("read_workspace", {"topic": "models"}, await _run())
    assert got["models_you_can_pick"] == {"anthropic": ["claude-haiku-4-5", "claude-sonnet-5-5"],
                                          "gemini": ["gemini-3-flash-preview"]}


async def test_a_model_the_workspace_offers_goes_to_the_review(client, role):
    agent = await _agent(name="Front desk", model="claude-haiku-4-5")
    out, _, _ = await _check([{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                               "fields": {"model": "gemini-3-flash-preview"}}])
    assert out["outcome"] == "review"


async def test_a_model_nothing_here_serves_is_said_before_the_review(client, role):
    agent = await _agent(name="Front desk", model="claude-haiku-4-5")
    out, _, _ = await _check([{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                               "fields": {"model": "synapse.nova-pro"}}])
    assert out["outcome"] == "said" and out["calls"] == []
    assert '"synapse.nova-pro" is not a model this workspace can use' in out["say"]
    assert "gemini-3-flash-preview" in out["say"]


async def test_a_list_that_cannot_be_read_refuses_nothing(client, role):
    async def broken():
        raise RuntimeError("providers down")

    model_choices.set_model_choices(broken)
    agent = await _agent(name="Front desk", model="claude-haiku-4-5")
    out, _, _ = await _check([{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                               "fields": {"model": "anything-new"}}])
    assert out["outcome"] == "review"
    got, _ = await _call("read_workspace", {"topic": "models"}, await _run())
    assert "models_you_can_pick" not in got and "error" not in got, json.dumps(got)[:300]


async def test_self_hosted_s_list_is_what_the_model_picker_offers(monkeypatch):
    """`/api/models`' providers that answer, with their models; one with no key, or none listed, is not offered."""
    from core.routes import data

    async def get_models(refresh=False):
        return {"providers": {"anthropic": {"available": True, "models": ["claude-haiku-4-5"]},
                              "grok": {"available": False, "models": ["grok-3"]},
                              "ollama": {"available": True, "models": []}}}

    monkeypatch.setattr(data, "get_models", get_models)
    model_choices.set_model_choices(None)
    assert await model_choices.choices() == {"anthropic": ["claude-haiku-4-5"]}
    assert model_choices.offers({"anthropic": ["claude-haiku-4-5"]}, "claude-haiku-4-5")
    assert not model_choices.offers({"anthropic": ["claude-haiku-4-5"]}, "synapse.nova-pro")
    assert model_choices.offers(None, "anything")


@pytest.fixture
def cloud_like():
    async def choices():
        return {"synapse": ["synapse.nova-pro", "synapse.nova-lite", "synapse.gpt-oss-120b-1-0"],
                "a": ["x.llama-3"], "b": ["y.llama-3"]}
    model_choices.set_model_choices(choices)


@pytest.mark.parametrize("said", ["nova-pro", "nova pro", "Nova Pro", "synapse.nova-pro"])
async def test_a_model_named_by_its_own_part_is_the_one_it_names(client, role, cloud_like, said):
    """F959 (P2's dev after-pass, Nova Pro, chg-04): the planner wrote `nova-pro`, and the check refused it with a list
    holding exactly one match, `synapse.nova-pro`. One choice whose own id is what was said is that choice; the review
    shows the full id, so the person approves the exact model."""
    agent = await _agent(name="Front desk", model="claude-haiku-4-5")
    out, _, _ = await _check([{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                               "fields": {"model": said}}])
    assert out["outcome"] == "review", out
    assert out["calls"] == [["update_agent", {"agent_id": agent["id"], "fields": {"model": "synapse.nova-pro"}}]]
    assert "model: synapse.nova-pro" in out["review"]


@pytest.mark.parametrize("said", ["nova", "llama-3"])
async def test_a_model_that_names_none_or_several_is_still_refused(client, role, cloud_like, said):
    agent = await _agent(name="Front desk", model="claude-haiku-4-5")
    out, _, _ = await _check([{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                               "fields": {"model": said}}])
    assert out["outcome"] == "said" and out["calls"] == []
    assert f'"{said}" is not a model this workspace can use' in out["say"]


def test_a_model_is_named_by_any_of_its_own_suffixes():
    """SAI2's review (nit): `us.anthropic.claude-haiku-4-5` is "claude-haiku-4-5" and "anthropic.claude-haiku-4-5"
    too; a dot inside the id itself ("gpt-4.1-mini") is never a provider's prefix to cut at."""
    found = {"bedrock": ["us.anthropic.claude-haiku-4-5"], "openai": ["gpt-4.1-mini"]}
    assert model_choices.named(found, "claude-haiku-4-5") == "us.anthropic.claude-haiku-4-5"
    assert model_choices.named(found, "anthropic.claude-haiku-4-5") == "us.anthropic.claude-haiku-4-5"
    assert model_choices.named(found, "GPT-4.1-mini") == "gpt-4.1-mini"
