"""The prompt writer, for an agent that answers from knowledge bases (B1b, D161).

Its METHODOLOGY rule "Tool vs. Knowledge" means the model's own knowledge:
"use knowledge only for general reasoning". For an agent with knowledge bases
that reads as the opposite of what it should do. Given the bases, the writer
is told to replace that rule: answer from them first, quote and cite each
passage by its label, and say when they hold no answer. An agent without
bases gets exactly the request it got before.
"""
from __future__ import annotations

BODY = {"description": "A support agent for our hosting customers", "agent_type": "conversational",
        "tools": ["search_web"]}


def _sent(fake_llm) -> tuple[str, str]:
    call = fake_llm.calls[-1]
    prompt = call["prompt_msg"]
    return prompt.split("\n", 1)[1], call["sys_prompt"]  # the first line is the date and time


async def test_an_agent_without_bases_gets_the_same_request(client, fake_llm):
    fake_llm.set_default("prompt")
    assert (await client.post("/api/agents/generate-prompt", json=BODY)).status_code == 200
    before = _sent(fake_llm)
    assert (await client.post("/api/agents/generate-prompt", json={**BODY, "knowledge_bases": []})).status_code == 200
    assert _sent(fake_llm) == before
    assert "KNOWLEDGE BASES" not in before[0]


async def test_with_bases_the_rule_is_replaced(client, fake_llm):
    fake_llm.set_default("prompt")
    bases = [{"name": "Contracts", "description": "Customer contracts and their renewals."},
             {"name": "Pricing", "description": ""}]
    resp = await client.post("/api/agents/generate-prompt", json={**BODY, "knowledge_bases": bases})
    assert resp.status_code == 200, resp.text
    prompt, system = _sent(fake_llm)
    section = prompt[prompt.index("━━━ KNOWLEDGE BASES ━━━"):]
    assert "- Contracts: Customer contracts and their renewals." in section and "- Pricing" in section
    assert "Tool vs. Knowledge" in section and "[K2]" in section
    assert "search_knowledge" not in prompt and "read_knowledge" not in prompt, "no tool names (the writer's rules)"
    assert "Tool vs. Knowledge" in system, "the writer's own instructions are unchanged"


async def test_bases_are_bounded(client, fake_llm):
    fake_llm.set_default("prompt")
    bases = [{"name": f"Base {i}", "description": "x" * 1000} for i in range(30)]
    resp = await client.post("/api/agents/generate-prompt", json={**BODY, "knowledge_bases": bases})
    assert resp.status_code == 200, resp.text
    prompt, _ = _sent(fake_llm)
    assert "- Base 9:" in prompt and "- Base 10:" not in prompt
    assert "x" * 301 not in prompt
