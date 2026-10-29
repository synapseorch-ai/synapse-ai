"""The knowledge bases an agent answers from, held to one check at every door (F1223, BLD1).

The agent editor offers only the workspace's active bases, while Knowledge is on, and its save checked only that no
more than ten were picked. The builder could not give an agent a base at all. Now both doors, `POST /api/agents` and
the builder's `create_agent` / `create_agents` / `update_agent`, ask `refusal` before they save: Knowledge is on, and
each base that is *added* is here and active. A base the agent already had is never asked about again, so an agent
whose base was deleted or turned off since still saves, and runs, as it did.
"""
from __future__ import annotations

from typing import Any


def ids(value: Any) -> list[str]:
    """Each id once, in order, blanks dropped, as `Agent.knowledge` keeps them."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(v).strip() for v in value if str(v or "").strip()))


async def refusal(picked: Any, before: Any = None) -> str | None:
    """Why `picked` cannot be the agent's knowledge, in the editor's words, or None. `before` is what it had."""
    from core.models import AGENT_KNOWLEDGE_MAX

    if picked is not None and not isinstance(picked, list):
        return "'knowledge' is a list of knowledge base ids, from list_knowledge_bases."
    kept = ids(picked)
    if len(kept) > AGENT_KNOWLEDGE_MAX:
        return f"An agent answers from at most {AGENT_KNOWLEDGE_MAX} knowledge bases."
    added = [kb_id for kb_id in kept if kb_id not in ids(before)]
    if not added:
        return None
    from core import features

    if not await features.is_on("knowledge"):
        return features.off_sentence("knowledge")
    from core.knowledge import bases

    for kb_id in added:
        kb = await bases.get_base(kb_id)
        if kb is None:
            return f"There is no knowledge base '{kb_id}' here: use an id list_knowledge_bases returns."
        if kb.status != "active":
            return f"The knowledge base '{kb.name}' ({kb_id}) is turned off: turn it on in Knowledge first."
    return None
