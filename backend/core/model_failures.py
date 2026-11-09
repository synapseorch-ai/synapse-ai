"""What a failed model call is, in a sentence a person can act on (F275, F512).

Our words, never the provider's (D118): a provider's message can carry a key's prefix, an account id or its own
jargon. The one exception is the platform's own gateway, whose refusals (a 4xx: an empty model wallet, a model that
cannot read images) are already a sentence written for a person — `llm_providers` keeps that sentence, after its
`GATEWAY_REFUSED` prefix, and drops the gateway's JSON.
"""
from __future__ import annotations

#: What each kind of model failure means for the person (F275), by `LLMError.code`.
MODEL_FAILURE = {
    "quota_exhausted": "The model provider refused it: its credit or quota is exhausted. Top up or raise "
                       "the limit with the provider; retrying will not help until then.",
    "provider_refused": "The model provider refused it — for example a key it does not accept or a model "
                        "it does not serve. Check the agent's model and the workspace's model settings.",
    "rate_limited": "The model provider is limiting requests right now. Try again shortly.",
    "provider_unavailable": "The model provider could not be reached or failed. Try again shortly.",
    "model_no_answer": "The model answered with nothing usable (empty, blocked, or only reasoning).",
}

#: How `llm_providers` begins a refusal the platform's gateway made itself; what follows is its sentence.
GATEWAY_REFUSED = "Model gateway refused the call: "


def failure_sentence(exc: BaseException) -> str:
    """The sentence for one failed model call: the gateway's own, or ours for its kind; never the provider's."""
    from core.llm_providers import LLMError

    if isinstance(exc, LLMError):
        message = str(exc)
        if exc.provider == "gateway" and message.startswith(GATEWAY_REFUSED):
            return message.removeprefix(GATEWAY_REFUSED)
        if exc.code in MODEL_FAILURE:
            return MODEL_FAILURE[exc.code]
    return "The model call failed."


def recorded_sentence(reason: str, code: str | None) -> str:
    """`failure_sentence` for a model call that failed in a step (F1483): the step's history entry keeps its reason
    (`run_error.reason_of`, "LLMError: …") and its `model_failure` code, not the exception. The gateway's refusal is
    its own sentence, after `GATEWAY_REFUSED`, which only the gateway writes, wherever the reason holds it; any other
    is ours for its kind."""
    at = str(reason or "").find(GATEWAY_REFUSED)
    if at >= 0:
        return str(reason)[at + len(GATEWAY_REFUSED):].strip() or "The model call failed."
    return MODEL_FAILURE.get(code or "", "The model call failed.")
