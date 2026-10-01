"""Draft test questions from a knowledge base's own passages, and what drafting costs (V1, D168).

**Drafts are suggestions.** Nothing here saves anything: a person accepts or
edits each draft, and only their save writes a set — the rule of
`core/evals/generate.py`.

**Where the questions come from.** Passages of the serving snapshot, sampled
documents first and then passages within each (a seeded `random.Random`, so a
seed draws the same sample again): passages too short to ask about, repeated
word for word, or already asked about by the set are left out. Three kinds:

* `direct` — a question one passage answers, asked plainly;
* `paraphrase` — the same, in other words: at most `MAX_OVERLAP` of the
  question's content words may come from the passage (character pairs for
  Chinese, Japanese and Korean), asked once more if it is closer, dropped if it
  stays closer;
* `multi_hop` — a question needing two passages of different documents that
  share rare terms (inverse document frequency over the sample).

The model reads each passage's own text (`content`; a table group's header is
in `context` and would make a quote unfindable) and copies the words that
answer the question. A quote must be found in its passage — exactly, or with
only spacing and punctuation differing — and its place becomes the evidence's
span; a draft whose quote is not found is dropped and counted.

**Cost.** One chat call per draft (two for a paraphrase asked again), priced
before drafting by `estimate_drafts` from the rate card; a model the card does
not price is said to be unpriced, never $0. The calls go through the chat path
(`generate_response`, source "evals"), so their usage is logged there.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import EvalDraftRequest, EvalDrafts, EvalEstimate
    from core.knowledge.kb_settings import KnowledgeBase

_log = get_logger(__name__)

TIMEOUT_SECONDS = 45.0
CONCURRENCY = 3
#: Passages shorter than this many tokens are not asked about.
MIN_PASSAGE_TOKENS = 30
#: Passages drawn per request, before any is asked about.
POOL = 200
#: A paraphrase may take at most this share of its content words from the passage.
MAX_OVERLAP = 0.35
#: A multi-hop pair shares at least this many rare terms.
MIN_SHARED_TERMS = 2
QUOTE_MAX = 300
#: Tokens a draft's prompt adds to its passages, and a reply's length (for the estimate).
PROMPT_TOKENS = 260
REPLY_TOKENS = 120
#: Paraphrases asked again, on average (for the estimate).
PARAPHRASE_RETRY = 1.3

SYSTEM_PROMPT = ("You write test questions for a document search engine, from passages you are shown. "
                 "You output ONLY a JSON object. No prose.")

_STYLE = {
    "direct": "Ask it plainly, as someone who knows the subject would.",
    "paraphrase": ("Ask it in your own words, as someone who has not read the passage would: do not reuse its "
                   "distinctive terms, names or phrasing where an everyday word would do."),
}

_STOP = frozenset("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our
out over own same she should so some such than that the their them then there these they this those through to too
under until up very was we were what when where which while who whom why will with would you your yours yourself
""".split())
_WORD = re.compile(r"\w+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


# ── the sample ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Drawn:
    """A passage a draft may ask about."""

    chunk_id: str
    doc_id: str
    path: str
    doc_sha256: str | None
    char_start: int
    char_end: int
    content: str
    kind: str = "text"
    attachment: str | None = None


def default_model(settings: Mapping[str, Any]) -> str:
    """The model that writes drafts when none is named: the judge model, else the workspace's (as `propose`)."""
    model = str(settings.get("judge_model") or "").strip()
    if model == "default":
        model = ""
    return model or str(settings.get("model") or "").strip()


def _tokens(text: str) -> int:
    from core.knowledge.chunker import estimate_tokens

    return estimate_tokens(text)


async def sample(kb: KnowledgeBase, rng: random.Random, *, exclude: Sequence[Mapping[str, Any]] = (),
                 size: int = POOL) -> list[Drawn]:
    """Up to `size` passages of `kb`'s serving snapshot: documents first, then passages within each, in turns."""
    from sqlalchemy import select

    from core.knowledge import evals_metrics as EM
    from core.knowledge.chunker import ATTACHMENT_KEY
    from core.knowledge.snapshots import active_snapshots
    from core.store import session
    from core.store.models import KnowledgeChunkDB as C
    from core.tenancy import get_tenant

    snap = (await active_snapshots([kb.id])).get(kb.id)
    if snap is None:
        raise ValueError("Nothing in this knowledge base is indexed yet, so there is nothing to ask about.")
    async with session() as s:
        light = (await s.execute(select(C.chunk_id, C.doc_id, C.tokens).where(
            C.tenant_id == get_tenant(), C.kb_id == kb.id, C.snapshot_id == snap, C.role == "leaf",
            C.kind != "sheet_card"))).all()
    by_doc: dict[str, list[str]] = {}
    for chunk_id, doc_id, tokens in sorted(light, key=lambda r: r[0]):
        if not tokens or tokens >= MIN_PASSAGE_TOKENS:  # an uncounted passage is measured once it is read
            by_doc.setdefault(doc_id, []).append(chunk_id)
    docs = sorted(by_doc)
    rng.shuffle(docs)
    for ids in by_doc.values():
        rng.shuffle(ids)
    picked: list[str] = []
    turn = 0
    while len(picked) < size * 2 and any(turn < len(by_doc[d]) for d in docs):
        picked.extend(by_doc[d][turn] for d in docs if turn < len(by_doc[d]))
        turn += 1
    async with session() as s:
        rows = {r.chunk_id: r for r in (await s.execute(select(C).where(
            C.tenant_id == get_tenant(), C.chunk_id.in_(picked[:size * 2])))).scalars().all()}
    seen: set[str] = set()
    out: list[Drawn] = []
    for chunk_id in picked:
        r = rows.get(chunk_id)
        if r is None or (not r.tokens and _tokens(r.content or "") < MIN_PASSAGE_TOKENS):
            continue
        digest = hashlib.sha256(" ".join((r.content or "").split()).lower().encode("utf-8")).hexdigest()
        passage = EM.Passage.of(r)
        if digest in seen or any(EM.covers(passage, ev) for ev in exclude):
            continue
        seen.add(digest)
        out.append(Drawn(chunk_id=r.chunk_id, doc_id=r.doc_id, path=r.path or "", doc_sha256=r.doc_sha256,
                         char_start=r.char_start, char_end=r.char_end, content=r.content or "", kind=r.kind,
                         attachment=(r.meta or {}).get(ATTACHMENT_KEY)))
        if len(out) >= size:
            break
    return out


# ── words ─────────────────────────────────────────────────────────────────────

def content_words(text: str) -> set[str]:
    """What overlap counts: words of three letters or more that are not stop words; for Chinese, Japanese and
    Korean, pairs of characters."""
    if _CJK.search(text or ""):
        chars = [c for c in text if _CJK.match(c)]
        return {a + b for a, b in zip(chars, chars[1:])}
    return {w for w in (m.group(0).lower() for m in _WORD.finditer(text or "")) if len(w) >= 3 and w not in _STOP}


def overlap(question: str, passage: str) -> float:
    """The share of the question's content words found in the passage."""
    asked = content_words(question)
    if not asked:
        return 0.0
    return round(len(asked & content_words(passage)) / len(asked), 4)


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Where `quote` is in `text`: exactly, else its words in order with only spacing and punctuation between."""
    quote = (quote or "").strip().strip("\"'“”‘’").strip()
    if not quote:
        return None
    at = text.find(quote)
    if at >= 0:
        return at, at + len(quote)
    words = [w for w in _WORD.findall(quote)]
    if len(words) < 3:
        return None
    match = re.search(r"\W+".join(re.escape(w) for w in words), text, flags=re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def partner(anchor: Drawn, pool: Sequence[Drawn]) -> Drawn | None:
    """The passage of another document sharing the most rare terms with `anchor`, if it shares enough."""
    vocab = [content_words(p.content) for p in pool]
    df: dict[str, int] = {}
    for words in vocab:
        for w in words:
            df[w] = df.get(w, 0) + 1
    n = len(pool)
    mine = content_words(anchor.content)
    best, best_score = None, 0.0
    for p, words in zip(pool, vocab):
        if p.chunk_id == anchor.chunk_id or p.doc_id == anchor.doc_id:
            continue
        shared = [w for w in mine & words if df[w] < max(2, n // 4)]
        if len(shared) < MIN_SHARED_TERMS:
            continue
        score = sum(math.log((n + 1) / df[w]) for w in shared)
        if score > best_score or (score == best_score and best is not None and p.chunk_id < best.chunk_id):
            best, best_score = p, score
    return best


# ── one draft ─────────────────────────────────────────────────────────────────

def _block(name: str, passage: Drawn) -> str:
    from core.knowledge.citations import sanitize

    return f"{name} (from {passage.path}):\n<<{name.upper()}>>\n{sanitize(passage.content)}\n<<END {name.upper()}>>"


def prompt(kind: str, passages: Sequence[Drawn], *, retry_note: str = "") -> str:
    if kind == "multi_hop":
        a, b = passages
        body = (f"{_block('Passage A', a)}\n\n{_block('Passage B', b)}\n\n"
                "Write one question that needs a fact from Passage A and a fact from Passage B to answer: neither "
                "passage alone is enough. Ask it plainly.\n"
                f"Then copy, word for word, the words of each passage that answer it (at most {QUOTE_MAX} characters "
                "each).\n"
                'Respond with ONLY a JSON object: {"question": "...", "answer": "<one sentence>", '
                '"quotes": ["<exact words from Passage A>", "<exact words from Passage B>"]}')
    else:
        body = (f"{_block('Passage', passages[0])}\n\n"
                f"Write one question a person could ask that this passage answers. {_STYLE[kind]}\n"
                f"Then copy, word for word, the shortest words of the passage that answer it (at most {QUOTE_MAX} "
                "characters).\n"
                'Respond with ONLY a JSON object: {"question": "...", "answer": "<one sentence>", '
                '"quote": "<exact words from the passage>"}')
    return body + (f"\n\n{retry_note}" if retry_note else "")


def parse_reply(text: str) -> dict | None:
    """The reply's JSON object, or None. Never guesses past a missing one."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


async def ask(model: str, prompt_text: str, system: str, settings: Mapping[str, Any], *, session_id: str,
              agent_id: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """One chat call through the chat path (its usage is logged there). Raises on a failure or a timeout."""
    from core.llm_providers import detect_mode_from_model, generate_response

    text = await asyncio.wait_for(generate_response(
        prompt_msg=prompt_text, sys_prompt=system, mode=detect_mode_from_model(model), current_model=model,
        current_settings=dict(settings), session_id=session_id, agent_id=agent_id, source="evals"),
        timeout=timeout)
    return text if isinstance(text, str) else ""


def evidence(passage: Drawn, quote: str) -> dict | None:
    """The evidence a quote of `passage` is, or None when the quote is not in it."""
    found = locate(quote, passage.content)
    if found is None:
        return None
    start, end = found
    exact = passage.content[start:end]
    if passage.kind == "text":
        span = (passage.char_start + start, passage.char_start + end)
    else:  # a table's text is laid out, not a slice of the document's: its span is the whole passage
        span = (passage.char_start, passage.char_end)
    return {"doc_id": passage.doc_id, "path": passage.path, "attachment": passage.attachment,
            "doc_sha256": passage.doc_sha256, "char_start": span[0], "char_end": span[1],
            "quote": exact[:1000]}


async def draft_one(kind: str, passages: Sequence[Drawn], *, model: str, settings: Mapping[str, Any],
                    session_id: str) -> tuple[dict | None, str | None]:
    """(the draft, None), or (None, why it was dropped)."""
    note = ""
    for attempt in range(2 if kind == "paraphrase" else 1):
        try:
            reply = await ask(model, prompt(kind, passages, retry_note=note), SYSTEM_PROMPT, settings,
                              session_id=session_id, agent_id="knowledge_eval_writer")
        except asyncio.TimeoutError:
            return None, "timed_out"
        except Exception as exc:  # noqa: BLE001 — a failed call drops one draft, never the request
            _log.warning(f"knowledge eval draft failed: {exc}")
            return None, "failed"
        data = parse_reply(reply)
        question = str((data or {}).get("question") or "").strip()
        if not question:
            return None, "unreadable"
        quotes = data.get("quotes") if kind == "multi_hop" else [data.get("quote")]
        if not isinstance(quotes, list) or len(quotes) != len(passages):
            return None, "quote_not_found"
        found = [evidence(p, str(q or "")) for p, q in zip(passages, quotes)]
        if any(ev is None for ev in found):
            return None, "quote_not_found"
        share = overlap(question, " ".join(p.content for p in passages))
        if kind == "paraphrase" and share > MAX_OVERLAP:
            reused = sorted(content_words(question) & content_words(passages[0].content))[:8]
            note = (f"Your question reused these words of the passage: {', '.join(reused)}. Ask again, in other "
                    "words, keeping the same answer.")
            continue
        return {"kind": kind, "question": question[:2000], "reference_answer": str(data.get("answer") or "")[:4000],
                "evidence": found, "overlap": share}, None
    return None, "too_close_to_passage"


async def draft(kb: KnowledgeBase, body: EvalDraftRequest, settings: Mapping[str, Any], *,
                exclude: Sequence[Mapping[str, Any]] = ()) -> EvalDrafts:
    """Draft `body`'s questions from `kb`'s passages. Saves nothing."""
    from core.knowledge.api_models import EvalDrafts

    model = body.model or default_model(settings)
    if not model:
        raise ValueError("Choose a model to write the questions.")
    rng = random.Random(body.seed if body.seed is not None else random.SystemRandom().randrange(2 ** 31))
    pool = await sample(kb, rng, exclude=exclude)
    dropped: dict[str, int] = {}
    jobs: list[tuple[str, list[Drawn]]] = []
    free = list(pool)
    for kind, count in (("direct", body.direct), ("paraphrase", body.paraphrase), ("multi_hop", body.multi_hop)):
        for _ in range(count):
            if not free:
                dropped["no_passage"] = dropped.get("no_passage", 0) + 1
                continue
            anchor = free.pop(0)
            if kind != "multi_hop":
                jobs.append((kind, [anchor]))
                continue
            other = partner(anchor, free)
            if other is None:
                dropped["no_partner"] = dropped.get("no_partner", 0) + 1
                continue
            free.remove(other)
            jobs.append((kind, [anchor, other]))
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(kind: str, passages: list[Drawn]):
        async with gate:
            return await draft_one(kind, passages, model=model, settings=settings,
                                   session_id=f"knowledge-eval:{kb.id}")

    drafts = []
    for made, why in await asyncio.gather(*(one(kind, passages) for kind, passages in jobs)):
        if made is None:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            drafts.append(made)
    return EvalDrafts(drafts=drafts, model=model, dropped=dropped)


# ── what drafting costs ───────────────────────────────────────────────────────

async def chat_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """What `model` charges for the tokens, by the rate card; None when the card does not price it."""
    from core import usage_tracker

    await usage_tracker._ensure_pricing()
    if usage_tracker._resolve_pricing_entry(model) is None:
        return None
    return usage_tracker.calculate_cost(model, input_tokens, output_tokens)


async def estimate_drafts(kb: KnowledgeBase, body: EvalDraftRequest, settings: Mapping[str, Any]) -> EvalEstimate:
    from core.knowledge.api_models import EvalCostLine, EvalEstimate

    model = body.model or default_model(settings)
    if not model:
        raise ValueError("Choose a model to write the questions.")
    passage = max(MIN_PASSAGE_TOKENS, kb.chunking.target_tokens)
    calls = body.direct + body.paraphrase * PARAPHRASE_RETRY + body.multi_hop
    input_tokens = round((body.direct + body.paraphrase * PARAPHRASE_RETRY) * (PROMPT_TOKENS + passage)
                         + body.multi_hop * (PROMPT_TOKENS + 2 * passage))
    output_tokens = round(calls * REPLY_TOKENS)
    usd = await chat_usd(model, input_tokens, output_tokens)
    line = EvalCostLine(part="drafts", model=model, calls=math.ceil(calls), input_tokens=input_tokens,
                        output_tokens=output_tokens, est_usd=usd)
    return EvalEstimate(lines=[line], total_est_usd=usd or 0.0, unpriced=[model] if usd is None else [])
