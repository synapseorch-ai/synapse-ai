"""V1's test kit: a knowledge base with passages worth asking about, and a fake question writer.

The documents go in through the real path — a Vault file, its document row,
the ingest job — so drafts and runs read the rows production writes. Two pairs
of documents share a rare term ("Lisbon", "Beacon"), which is what a multi-hop
question needs. Use with the `knowledge_store` fixture.
"""
from __future__ import annotations

import json
import re

POLICIES = {
    "policies/refunds.md": (
        "# Refunds\n\nA customer who cancels during the first thirty days of a subscription receives a full refund "
        "to the original card. Refunds are issued by the billing team in Lisbon and arrive in five working days."),
    "policies/hosting.md": (
        "# Hosting\n\nThe hosting fee for the Atlas plan is billed monthly in advance and covers two regions. "
        "Customers on the Atlas plan may add a third region for an extra fee set by the billing team in Lisbon."),
    "policies/support.md": (
        "# Support\n\nThe support desk answers every ticket within one business day, and urgent tickets marked "
        "with the Beacon flag go to the duty officer within two hours, around the clock."),
    "policies/security.md": (
        "# Security\n\nStored data is encrypted at rest with keys rotated quarterly, and any access by engineers "
        "requires a Beacon approval from the duty officer on call."),
}


async def write(kb, path: str, text: str) -> str:
    """A Vault file, its document row and its queued ingest job (run them with `jobs.drain`)."""
    from _fakes.knowledge_env import write_vault
    from core.knowledge import dispatch, documents

    write_vault(path, text)
    row, queued = await documents.upsert_for_path(kb, path, source="vault", sha256=None, size=None)
    if queued:
        await dispatch.enqueue(kb.id, "upsert", "vault_write", path=row.path, doc_id=row.id)
    return row.id


async def policies(jobs, **kw):
    """A knowledge base on `policies/` holding POLICIES, indexed."""
    from core.knowledge import bases, documents
    from core.knowledge.api_models import KnowledgeBaseCreate
    from core.knowledge.jobs import run_ingest_job

    kb = await bases.create_base(KnowledgeBaseCreate(name="Policies", folder="policies", **kw))
    for path, text in POLICIES.items():
        await write(kb, path, text)
    await jobs.drain(run_ingest_job)
    assert {r.status for r in await documents.list_for(kb.id)} == {"indexed"}
    return kb


_PASSAGE = re.compile(r"<<(PASSAGE(?: [AB])?)>>\n(.*?)\n<<END \1>>", re.S)


def passages_in(prompt: str) -> list[str]:
    return [m.group(2) for m in _PASSAGE.finditer(prompt)]


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]


class Writer:
    """A question writer that reads the passages it is shown: it quotes each one's first sentence. `questions`
    are asked in turn (the last one repeats; a callable is given the passages); `reply` overrides everything."""

    def __init__(self, questions=("What does the policy say about it?",), *, reply: str | None = None,
                 quote=first_sentence):
        self.questions = list(questions)
        self.reply = reply
        self.quote = quote
        self.calls: list[dict] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.reply is not None:
            return self.reply
        question = self.questions[min(len(self.calls) - 1, len(self.questions) - 1)]
        found = passages_in(kwargs.get("prompt_msg") or "")
        if callable(question):
            question = question(found)
        if len(found) == 2:
            return json.dumps({"question": question, "answer": "Both say so.",
                               "quotes": [self.quote(p) for p in found]})
        return json.dumps({"question": question, "answer": "It says so.", "quote": self.quote(found[0])})
