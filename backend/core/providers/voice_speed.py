"""Models fast enough for a phone call (V15): the "fast for voice" list.

A call's turn gap is about a second end to end (Twilio's own target is 1,115 ms mouth to ear), and our share is
the model's first words. A number whose answering agent runs on a model not listed here shows a quiet note on its
screen (VOX1c). It never blocks: an org may prefer a smart, slow answer.

Kept beside the provider table rather than in it: the table's entries are pinned by its tests and cloud derives
its key lists from it (PROV1). Matched by model-id prefix, as models are routed (`<provider>.<model>`).
Measured or vendor-stated:

* Groq and Cerebras serve open models at hundreds to thousands of tokens a second (Artificial Analysis: Cerebras
  gpt-oss-120b, 280 ms to first token).
* GLM-4.7-Flash answered in 0.7 s through OpenRouter with thinking off (PROV1, 2026-10-10; 7.7 s with it high).
* Amazon Nova Micro and Lite are the managed models' fast end.

A model that writes its reasoning before answering is slow on a call however fast it streams: Claude Haiku's
first words came at ~7 s behind a reasoning block (OAI1). Run voice agents with reasoning off.
"""
from __future__ import annotations

#: Model-id prefixes fast enough for a call.
FAST_FOR_VOICE: tuple[str, ...] = (
    "groq.",
    "cerebras.",
    "openrouter.z-ai/glm-4.7-flash",
    "zai.glm-4.7-flash",
    "bedrock.amazon.nova-micro",
    "bedrock.amazon.nova-lite",
    "bedrock.us.amazon.nova-micro",
    "bedrock.us.amazon.nova-lite",
    "synapse.nova-micro",
    "synapse.nova-lite",
)


def fast_for_voice(model: str | None) -> bool:
    """Whether `model` is on the list (a quiet note when it is not; never a block)."""
    name = str(model or "").strip().lower()
    return bool(name) and any(name.startswith(prefix) for prefix in FAST_FOR_VOICE)
