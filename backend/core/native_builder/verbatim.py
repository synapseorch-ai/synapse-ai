"""Instructions the person pasted reach what the build makes whole (F1550, BLD3; MIG1's bug B).

A pasted Agents SDK export says exactly which text is which Agent's instructions (`export.agent_instructions`), and the
guide says they are copied word for word, however long. The copy was a model's: the planner retyped every Agent's
instructions into one `submit_plan` call and the prompt writer into `submit_prompts`, and nothing measured it. On a
46 KB export Haiku 4.5 kept 30–75% of six of eight Agents; Sonnet 4.6 kept 62% of one (its examples and code fences
gone) and flattened every prompt's typography. So code copies them, in two ways:

* **By reference.** A step's prompt or a new agent's instructions may hold `‹instructions of "<Agent name>"›`; code
  puts that Agent's instructions there. An Agent the code does not have is sent back, naming the ones it has.
* **Checked.** A text that copies most of an Agent's words, in order (`COPIED` of them, in runs of `RUN` words or
  more), but not the instructions whole, has the copy, from its first matched word to its last, replaced by the
  instructions themselves. What the text adds around the copy stays, and a `{{ … }}` the copy held is kept after it.
  Said on the card with the share of the words the copy had kept.

Each Agent goes to the one text most like it, and only a text whose most-like Agent it is: a prompt of the step's own
words, or one copying a little of an Agent, stays as written. An ordinary build has no export and nothing changes.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from core.native_builder.export import INSTRUCTIONS_REF, agent_instructions

#: The share of an Agent's words a text holds, in order, for it to be a copy of them.
COPIED = 0.3
#: The fewest words in a row that count as copied (fewer is two texts sharing a phrase).
RUN = 3
_WORD = re.compile(r"\w+")
_TEMPLATE = re.compile(r"\{\{.*?\}\}", re.S)


def sources(state: dict[str, Any] | None) -> dict[str, str]:
    """The pasted export's Agents' instructions, by name, from the run's `export_source`; {} in an ordinary build."""
    code = str((state or {}).get("export_source") or "")
    return agent_instructions(code) if code.strip() else {}


def _key(name: str) -> str:
    return " ".join(name.casefold().split())


def expand(text: str, agents: dict[str, str]) -> tuple[str, list[str]]:
    """`text` with each reference replaced by that Agent's instructions, and the names it gives that no Agent has."""
    by_key = {_key(n): v for n, v in agents.items()}
    unknown: list[str] = []

    def put(m: re.Match) -> str:
        found = by_key.get(_key(m.group(1)))
        if found is None:
            unknown.append(m.group(1).strip())
            return m.group(0)
        return found

    return INSTRUCTIONS_REF.sub(put, text), unknown


def _words(text: str) -> tuple[list[str], list[re.Match]]:
    found = list(_WORD.finditer(text))
    return [m.group().casefold() for m in found], found


def _copy(source: str, text: str) -> tuple[float, int, int]:
    """How much of `source`'s words `text` holds in order, and where that copy is in `text` (start, end)."""
    src, _ = _words(source)
    got, spans = _words(text)
    if not src or not got:
        return 0.0, 0, 0
    blocks = [b for b in difflib.SequenceMatcher(None, src, got, autojunk=False).get_matching_blocks()
              if b.size >= min(RUN, len(src))]
    if not blocks:
        return 0.0, 0, 0
    start, end = spans[blocks[0].b].start(), spans[blocks[-1].b + blocks[-1].size - 1].end()
    # The punctuation joined to the copy's first and last words is the copy's ("**Role**", "precise.").
    while start > 0 and not text[start - 1].isspace() and not text[start - 1].isalnum() and text[start - 1] != "}":
        start -= 1
    while end < len(text) and not text[end].isspace() and not text[end].isalnum() and text[end] != "{":
        end += 1
    return sum(b.size for b in blocks) / len(src), start, end


def whole(texts: dict[str, str], agents: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    """`texts` (by where each is: "step x", "agent X") with every reference expanded and every copy made whole; the
    errors to send back (a reference to no Agent) and the notes for the card (each copy made whole, and how much it
    had kept)."""
    if not agents:
        return dict(texts), [], []
    out, errors, notes = dict(texts), [], []
    names = ", ".join(f'"{n}"' for n in agents)
    for where, text in texts.items():
        expanded, unknown = expand(text or "", agents)
        out[where] = expanded
        if unknown:
            errors.append(f"{where}: " + "; ".join(f'‹instructions of "{u}"›' for u in unknown)
                          + f" names no Agent in the pasted code; its Agents are {names}: write one of those names")
    # Each Agent to the text most like it, where that text is most like it.
    scores = {(name, where): _copy(source, out[where]) for name, source in agents.items() for where in out
              if out[where] and source.strip() and source.strip() not in out[where]}
    best_text = {name: max(((s[0], w) for (n, w), s in scores.items() if n == name), default=(0.0, None))
                 for name in agents}
    best_agent = {where: max(((s[0], n) for (n, w), s in scores.items() if w == where), default=(0.0, None))
                  for where in out}
    for name, (share, where) in best_text.items():
        if where is None or share < COPIED or best_agent[where][1] != name:
            continue
        text = out[where]
        _, start, end = _copy(agents[name], text)
        kept = [t for t in _TEMPLATE.findall(text[start:end]) if t not in agents[name]]
        tail = "".join(f"\n\n{t}" for t in dict.fromkeys(kept))
        out[where] = text[:start] + agents[name] + tail + text[end:]
        notes.append(f"Copied “{name}”'s instructions whole into {where}: as written there, it had "
                     f"{min(share, 1.0):.0%} of their words.")
    return out, errors, notes
