"""The product features a request names, looked up before any model plans it (F1257, GUIDE1; D340 A, gather).

"Create an eval for my curiosity post generator" was planned as a workflow that grades another: the planner never
looked up what an eval is here. Now code finds each feature the person's message names, by the census rows' own
words (`feature_census`: a row's id, its label and its `names`, CAP3's table, so a feature added there is found
without touching this), and the planner's pack carries `features`, one entry each:

* what Synapse AI does there (the row's `does` and tools), or what it does not yet (`cant`), with the screen;
* what building a stand-in for it would look like (`imitation`), which a plan never is;
* the "?" panel's steps for it, and the docs page on it (`builder_help`).

No model is called for it. A message that names no feature leaves the pack as it was. What every plan is made of
(a census row's `plan_part`: workflows, agents, tools, runs and their logs) is never a feature here: "run",
"workflow" and "agent" are in nearly every request, and the pack lists what the workspace has of them already.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from core import feature_census
from core.feature_census import Screen


def _norm(text: str) -> str:
    """Lower case words joined by single spaces, each folded to its singular: "Golden Sets" → "golden set"."""
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", str(text or "")).casefold())
    return " ".join(_one(w) for w in words)


def _one(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words_of(screen: Screen) -> list[str]:
    """What a person may call a screen: its label, its id and its census names, each as `_norm` reads it."""
    said = [screen.label, screen.id.replace("_", " "), *screen.names]
    return list(dict.fromkeys(w for w in (_norm(s) for s in said) if w))


def named_in(text: str, also: Iterable[str] = (), census: tuple[Screen, ...] | None = None) -> list[Screen]:
    """The screens `text` names by a whole word or phrase, in the census's order; and those `also` names by id (a
    request's typed kind, F1255's eval)."""
    said = f" {_norm(text)} "
    extra = set(also)
    return [s for s in (census or feature_census.current())
            if s.id in extra or not s.plan_part and any(f" {w} " in said for w in words_of(s))]


def _reach(screen: Screen) -> str:
    return f"Synapse AI {screen.does}" if screen.does else ""


def features(text: str, also: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The pack's `features`: each feature the request names, with what is done there from here or not, what
    imitating it would look like, and the help and docs on it."""
    from core.builder_help import search, search_docs

    out = []
    for s in named_in(text, also):
        topic = next(iter(search(s.label, top=1)), None)
        doc = next(iter(search_docs(s.label, top=1)), None)
        out.append({
            "feature": s.label, "screen": s.href,
            **({"here": _reach(s), "tools": list(s.tools)} if s.does else {}),
            **({"not_from_here": s.cant} if s.cant else {}),
            **({"never_build": s.imitation} if s.imitation else {}),
            **({"help": {"title": topic["title"], "steps": topic["steps"], "screen": topic.get("screen")}}
               if topic else {}),
            **({"docs": doc["url"]} if doc else {}),
        })
    return out
