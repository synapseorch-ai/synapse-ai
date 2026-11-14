"""The GPT-6 family's window is read as 272K: where Bedrock's price for it doubles (F1593, BED2).

Bedrock bills every GPT-6 and GPT-6.1 model at twice its input-side rates and 1.5 times its output for the whole
request once the input exceeds 272,000 tokens ("Long-context rates apply to the full request when input exceeds
272,000 tokens", the GPT-6 cards, 2026-11). Their true windows are about a million tokens, and the engine had none
for `gpt-6`: unknown, so a step's budget (D339) would grow a conversation into the dearer tier without anyone
choosing it. 272K is the same figure the table already holds for `gpt-5` (OpenAI's input limit), and it is above
Synapse AI's 128K floor, so no GPT-6 model is ruled out for it.
"""
import pytest

from core.providers.windows import window_tokens


@pytest.mark.parametrize("model", [
    "gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra",
    "us.openai.gpt-6.1-sol", "openai.gpt-6-luna", "synapse.gpt-6.1-sol", "synapse.gpt-6-luna",
])
def test_a_gpt_6_model_is_read_at_272k(model):
    assert window_tokens(model) == 272_000


def test_gpt_5_is_unchanged():
    assert window_tokens("gpt-5.4") == window_tokens("us.openai.gpt-5.6-sol") == 272_000
