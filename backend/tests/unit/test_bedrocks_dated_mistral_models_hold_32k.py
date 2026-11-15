"""Bedrock's dated Mistral models hold 32K tokens, whatever Mistral's own API calls the same names (F1586).

Prod, 11-13 (TRI3): a Synapse AI build on `synapse.mistral-large` (Bedrock's `mistral.mistral-large-2402-v1:0`) was
refused at its planner's first call, twice. A Converse call to that model with a ~40k-token prompt is refused with
"This model's maximum context length is 32768 tokens" (TRI3's probe, 11-14). The window table read the id through its
`mistral-large` row, which is Mistral's own API (Large 2 and later: 128K), so the model menu and a step's context budget
took a 32K model for a 128K one. Bedrock's other 2024 Mistral models are 32K too: Mistral Small (24.02), Mistral 7B
Instruct and Mixtral 8x7B Instruct (their Bedrock model cards). Mistral Large 2 (24.07) on Bedrock stays 128K.
"""

from __future__ import annotations

from core.providers import window_tokens


def test_mistral_large_2402_is_32k_on_bedrock():
    for model in ("mistral.mistral-large-2402-v1:0", "bedrock.mistral.mistral-large-2402-v1:0", "mistral-large-2402"):
        assert window_tokens(model) == 32_768, model


def test_bedrocks_other_2024_mistral_models_are_32k():
    for model in ("mistral.mistral-small-2402-v1:0", "mistral.mistral-7b-instruct-v0:2",
                  "mistral.mixtral-8x7b-instruct-v0:1", "synapse.mistral-small-2402"):
        assert window_tokens(model) == 32_768, model


def test_the_newer_mistral_models_keep_their_windows():
    """Large 2 (24.07) and Mistral's own API names are untouched by the dated rows."""
    assert window_tokens("mistral.mistral-large-2407-v1:0") == 128_000
    assert window_tokens("mistral-large-latest") == 256_000
    assert window_tokens("mistral-small-latest") == 256_000
