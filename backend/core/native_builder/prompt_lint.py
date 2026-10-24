"""Prompt-form rules over Synapse AI's prompts, read by tests and by the Plan compiler (SAI2-P1, F773).

* **F757**: no prompt asks for a marker block (`[REASONING]`, `[WHY]` …) — the Claude 5 family refuses to write one
  before a tool call (CLIM1's measurement; `core/tools.py`'s action format says why in a plain sentence instead).
* **F764**: no sentence of twelve words or more is written in two places unless both name one library passage
  (`{{PROMPT:name}}`): a fix to one pasted copy leaves the others as they were.
* **F767**: a for-each loop's body step leads its prompt with the item it works on; "the next item" framing made a
  model continue a sequence instead of doing its own item's task.

A *place* is one prompt a model reads: `agent:<id>` (its system prompt), `step:<id>.<field>`, `fragment:<name>`.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any

from . import seed

#: A sentence this long or longer is a passage someone wrote, not a phrase two prompts happen to share.
SENTENCE_WORDS = 12
_SENTENCE = re.compile(r"(?<=[.!?:])\s+|\n+")
#: A bracketed all-caps block: `[REASONING]`, `[WHY]`, `[THOUGHT]` …
_MARKER = re.compile(r"\[[A-Z][A-Z_]{2,}\]")


def _places(agents: list[dict], orch: dict) -> dict[str, str]:
    places: dict[str, str] = {}
    for agent in agents:
        if isinstance(agent.get("system_prompt"), str):
            places[f"agent:{agent['id']}"] = agent["system_prompt"]
    for step in orch.get("steps") or []:
        for field in seed.PROMPT_FIELDS:
            if isinstance(step.get(field), str) and step[field]:
                places[f"step:{step['id']}.{field}"] = step[field]
    return places


def expanded_places() -> dict[str, str]:
    """Every prompt as it runs."""
    bundle = seed.bundled_definitions()
    return _places(bundle["agents"], bundle["orchestration"])


def raw_places(*, with_fragments: bool = False) -> dict[str, str]:
    """Every prompt as written, its library passages still named; with the passages themselves when asked."""
    agents_dir = os.path.join(os.path.dirname(seed.__file__), "agents")
    agents = []
    for name in sorted(os.listdir(agents_dir)):
        if name.endswith(".json"):
            with open(os.path.join(agents_dir, name), encoding="utf-8") as f:
                agents.append(json.load(f))
    with open(os.path.join(os.path.dirname(seed.__file__), "orchestration.json"), encoding="utf-8") as f:
        orch = json.load(f)
    places = _places(agents, orch)
    if with_fragments:
        from . import STEP_TYPE_CHEATSHEET

        places.update({f"fragment:{n}": seed.prompt_fragment(n) for n in seed.fragment_names()})
        places["fragment:STEP_TYPE_CHEATSHEET"] = STEP_TYPE_CHEATSHEET
    return places


def marker_blocks(places: dict[str, str]) -> list[tuple[str, str]]:
    """F757: each (place, marker) a prompt asks for."""
    return [(place, m) for place, text in places.items() for m in dict.fromkeys(_MARKER.findall(text))]


def duplicated_sentences(places: dict[str, str]) -> dict[str, list[str]]:
    """F764: each sentence of `SENTENCE_WORDS` words or more found in two places or more, with where."""
    where: dict[str, set[str]] = defaultdict(set)
    for place, text in places.items():
        for sentence in _SENTENCE.split(seed.FRAGMENT.sub(" ", text)):
            sentence = sentence.strip()
            if len(sentence.split()) >= SENTENCE_WORDS:
                where[sentence].add(place)
    return {s: sorted(p) for s, p in where.items() if len(p) > 1}


def loops_not_item_first(orch: dict[str, Any]) -> list[str]:
    """F767: the body steps of a for-each loop whose prompt's first line does not name the loop's item."""
    steps = {s.get("id"): s for s in orch.get("steps") or [] if isinstance(s, dict)}
    late: list[str] = []
    for loop in steps.values():
        if loop.get("type") != "loop" or not loop.get("loop_over"):
            continue
        item = re.escape(loop.get("loop_item_key") or "loop_item")
        names_it = re.compile(r"\{\{?\s*state\." + item + r"\b")
        for sid in loop.get("loop_step_ids") or []:
            body = steps.get(sid) or {}
            prompt = body.get("prompt_template") if body.get("type") in ("agent", "llm", "tool") else None
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            first = next(line for line in prompt.splitlines() if line.strip())
            if not names_it.search(first):
                late.append(sid)
    return late


#: A tool's name as a prompt writes it, in backticks.
_TICKED = re.compile(r"`([a-z][a-z0-9_]+)")
#: The engine's own tools an agent may be taught although its list does not name them.
_ENGINE_TOOLS = frozenset({"update_tasks", "remember", "load_skill"})


def known_tools() -> set[str]:
    """Every tool name this install knows: the builder's and the product's."""
    from core.builder_tools import BUILDER_TOOL_NAMES
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    return set(BUILDER_TOOL_NAMES) | {t.name for t in registry.all_tools()} | set(_ENGINE_TOOLS)


def tools_not_held(agents: list[dict] | None = None, orch: dict | None = None) -> list[tuple[str, str]]:
    """F800: each (place, tool) where a prompt an agent reads names, in backticks, a tool that agent does not hold:
    its system prompt and the prompts of the steps it runs. A model taught a tool it cannot call calls it (F803's
    class: gpt-oss called the `update_tasks` it was taught but not given, 3 times in 12). A name that is no tool
    (an op, a field) is not counted."""
    if agents is None or orch is None:
        bundle = seed.bundled_definitions()
        agents, orch = bundle["agents"], bundle["orchestration"]
    known = known_tools()
    found: list[tuple[str, str]] = []
    for agent in agents:
        held = set(agent.get("tools") or [])
        system = agent.get("system_prompt") or ""
        texts = [(f"agent:{agent['id']}", system, held)]
        for step in orch.get("steps") or []:
            if step.get("agent_id") != agent["id"]:
                continue
            # A step's `allowed_tools` is what its turn holds; the agent's own prompt is read there too (SAI2-P4).
            narrowed = step.get("allowed_tools")
            step_held = held & set(narrowed) if narrowed is not None else held
            if narrowed is not None:
                texts.append((f"step:{step['id']}.agent_prompt", system, step_held))
            texts.append((f"step:{step['id']}.prompt_template", step.get("prompt_template") or "", step_held))
        for place, text, may in texts:
            found += [(place, name) for name in dict.fromkeys(_TICKED.findall(text))
                      if name in known and name not in may and name not in _ENGINE_TOOLS]
    return found
