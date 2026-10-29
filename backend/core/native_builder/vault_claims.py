"""A builder reply never says a file is in the Vault without the Vault saying so (F1211, BLD1; CAP3's G4).

Asked where a workflow saved its chart, Synapse AI answered "The workflow saved two files in your vault:" and listed
two paths, with totals, that were never written. The reply is a model's own words, with no finish tool to refuse it,
so code reads it where every run ends (`change_tools.said_without_secret_asks`, F1093's net): a passage that says a
file was saved, written, created or stored, and names its path, is held to the Vault. When every path it names is
there, it stands. When one is not, the passage is replaced by code's lines, one per path: what is there, what a
saved workflow will write when it runs, and what could not be found.

`claimed(text)` is cheap and reads nothing: every run that ends asks it. `checked(text)` reads the Vault.
"""
from __future__ import annotations

import asyncio
import json
import re

#: A past-tense "it is there": saved, wrote, created, stored...; or "is in / can be found in".
_CLAIM = re.compile(r"\b(?:saved|wrote|written|created|stored|placed|exported|uploaded|generated|put)\b"
                    r"|\b(?:is|are) (?:now )?(?:in|at|under) (?:your |the )?vault\b|\bcan be found\b", re.I)
#: Words that make it a promise rather than a fact: "it will save", "each run writes".
_LATER = re.compile(r"\b(?:will|would|'ll|shall|when it runs|each run|every run|once it runs|after it runs)\b", re.I)
#: A path: in backticks, as a Vault mention, or bare with a folder and an extension. Never an address.
_QUOTED = re.compile(r"`([^`\n]+)`|@\[([^\]\n]+)\]")
_BARE = re.compile(r"(?<![\w/.:])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,5})\b")
_FILE = re.compile(r"^(?:[\w .-]+/)*[\w .-]+\.[A-Za-z0-9]{1,5}$")
_LIST = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s|^\s{2,}\S|^\s*```")


def _paths(block: str) -> list[str]:
    found = [a or b for a, b in _QUOTED.findall(block)] + _BARE.findall(block)
    return list(dict.fromkeys(p.strip() for p in found if "://" not in p and _FILE.match(p.strip())))


def _blocks(text: str) -> list[str]:
    """The reply's paragraphs, a code fence kept whole."""
    out, current, fenced = [], [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                out.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        out.append("\n".join(current))
    return out


def _sentences(block: str) -> list[str]:
    return re.split(r"(?<=[.!?:])\s+", block)


def _claims(block: str) -> bool:
    return any(_CLAIM.search(s) and not _LATER.search(s) for s in _sentences(block))


def regions(text: str) -> list[tuple[int, int, list[str]]]:
    """Each passage that claims files are there: (first block, last block, the paths it names). A claim's own
    paragraph runs on through the list, indented lines and code under it."""
    blocks = _blocks(text)
    out, i = [], 0
    while i < len(blocks):
        if not _claims(blocks[i]):
            i += 1
            continue
        j = i
        while j + 1 < len(blocks) and _LIST.match(blocks[j + 1]):
            j += 1
        paths = [p for b in blocks[i:j + 1] for p in _paths(b)]
        if paths:
            out.append((i, j, list(dict.fromkeys(paths))))
        i = j + 1
    return out


def claimed(text: str) -> bool:
    """Whether the reply says a named file is there (no reads)."""
    return isinstance(text, str) and bool(_CLAIM.search(text)) and bool(regions(text))


async def _there(path: str) -> bool:
    from core.storage import get_blob_store
    from core.vault import mention_key

    key = mention_key(path.removeprefix("vault/"))
    if key is None:
        return False
    try:
        return await asyncio.to_thread(get_blob_store().stat, key) is not None
    except Exception:  # noqa: BLE001 — a Vault that cannot be read has not said the file is there
        return False


async def _workflows_naming(paths: list[str]) -> set[str]:
    """The paths a saved workflow's steps name: it writes them when it runs."""
    from core.store.resources import load_orchestrations

    try:
        docs = await load_orchestrations()
    except Exception:  # noqa: BLE001
        return set()
    text = " ".join(json.dumps(d.get("steps") or [], default=str) for d in docs or [] if isinstance(d, dict))
    return {p for p in paths if p in text}


async def checked(text: str) -> tuple[str, bool]:
    """The reply with each passage whose paths are not all in the Vault said again by code, and whether it changed."""
    found = regions(text)
    if not found:
        return text, False
    every = list(dict.fromkeys(p for _, _, paths in found for p in paths))
    there = {p for p in every if await _there(p)}
    if there == set(every):
        return text, False
    later = await _workflows_naming([p for p in every if p not in there])
    blocks = _blocks(text)
    for first, last, paths in reversed(found):
        if set(paths) <= there:
            continue
        lines = [f"- `{p}` is in the Vault." if p in there
                 else f"- `{p}` is not in the Vault yet: the workflow saves it there when it runs." if p in later
                 else f"- I could not find `{p}` in the Vault: nothing was saved there." for p in paths]
        blocks[first:last + 1] = ["I checked the Vault:\n" + "\n".join(lines)]
    return "\n\n".join(blocks), True
