"""What a workflow's steps read from the run's state, what writes it, and the ways a run reaches a step with a key unset:
the one place every read check asks (F1621).

The checks that ask whether what a step reads is there when it runs (a step that breaks without a key, F1346 and
F1617; a prompt that reads one before it is written, U74; the validator's "nothing writes it"; the answer-field rules,
F1549, F1553, F1613, F1618 and F1620) each used to keep their own idea of who writes a key and walk the workflow their
own way. They read it here:
- what a step reads: `prompt_reads` (a model's prompt and a Retrieve query, where an unset key reads empty) and
  `hard_reads` (a Transform's unguarded code, a Loop's list and a Tool step's set arguments, which break without it);
- what writes a key: `own_writes` (a step itself), `writes` (with a container's body), `writable` (any step, or the
  engine) and `writers` (the steps whose answer a key holds, for the answer-field rules);
- the ways in: `Walk.unset(key)`, the steps a run can reach before any step writes the key, each with the step before
  it and which of its ways it took, and `way_in`, the branch a sentence names.

Plain dicts, so the validator (which holds the stored document) and the engine (`model_dump` of its steps) share it.
"""
from __future__ import annotations

import re

#: The keys the engine writes before any step: the run's input, and a workflow agent's conversation
#: (`run_orchestrator_agent`).
ENGINE_KEYS = frozenset({"user_input", "user_query", "history"})


# ── what writes a key ─────────────────────────────────────────────────────────────────────────────────────────────

def branch_chains(parallel: dict, by_id: dict[str, dict]) -> list[list[str]]:
    """The steps each branch runs, as `ParallelStepExecutor` resolves them.

    A branch listing one step follows that step's `next_step_id` chain up to the
    Parallel's own next step; a branch listing several runs exactly those.
    """
    convergence = parallel.get("next_step_id")
    chains: list[list[str]] = []
    for branch in parallel.get("parallel_branches") or []:
        if len(branch) != 1:
            chains.append(list(branch))
            continue
        chain, current = [branch[0]], branch[0]
        while True:
            nxt = (by_id.get(current) or {}).get("next_step_id")
            if not nxt or nxt == convergence or nxt in chain:
                break
            chain.append(nxt)
            current = nxt
        chains.append(chain)
    return chains


def writes(step_id: str, by_id: dict[str, dict], seen: set[str]) -> set[str]:
    """The keys a step writes: its output key, and a Loop's or a nested Parallel's body."""
    step = by_id.get(step_id)
    if not step or step_id in seen:
        return set()
    seen.add(step_id)
    keys = {step["output_key"]} if step.get("output_key") else set()
    if step.get("type") == "loop":
        for body_id in step.get("loop_step_ids") or []:
            keys |= writes(body_id, by_id, seen)
    elif step.get("type") == "parallel":
        for chain in branch_chains(step, by_id):
            for sid in chain:
                keys |= writes(sid, by_id, seen)
    return keys


def own_writes(step: dict) -> set[str]:
    """What a step writes itself (not its body): its output key, a Loop's index
    and a for-each Loop's item (before its body runs), a Human step's answer."""
    keys = {step["output_key"]} if step.get("output_key") else set()
    if step.get("type") == "loop":
        keys.add(step.get("loop_index_key") or "loop_index")
        if step.get("loop_over"):
            keys.add(step.get("loop_item_key") or "loop_item")
    if step.get("type") == "human":
        keys.add("human_response")
    return keys


def writable(steps: list) -> set[str]:
    """Every key a step of `steps` or the engine writes: the run's input, each output key, each Loop's index and a
    for-each Loop's item (F123, F212), and `human_response`, which the engine writes beside a Human step's own key
    so evaluators that check it still work."""
    keys: set[str] = {"user_input", "user_query"}
    for s in steps:
        ok = s.get("output_key")
        if ok:
            keys.add(ok)
        # A count or while loop has no item, so a body reading `loop_item` there is still told so.
        if s.get("type") == "loop":
            keys.add(s.get("loop_index_key") or "loop_index")
            if s.get("loop_over"):
                keys.add(s.get("loop_item_key") or "loop_item")
        if s.get("type") == "human":
            keys.add("human_response")
    return keys


def writers(steps: list) -> dict[str, list[dict]]:
    """Each key's writers: the steps whose answer it holds (`step_answers.keys_written`), in order."""
    from core.orchestration.step_answers import keys_written

    found: dict[str, list[dict]] = {}
    for s in steps:
        if isinstance(s, dict):
            for key in keys_written(s):
                found.setdefault(key, []).append(s)
    return found


# ── what a step reads ─────────────────────────────────────────────────────────────────────────────────────────────

def prompt_reads(step: dict) -> set[str]:
    """The keys a model's prompt names: an Agent's, an LLM's or a Tool step's
    template and a Retrieve step's query — where an unset key reads empty."""
    texts = []
    if step.get("type") in ("agent", "llm", "tool"):
        texts.append(step.get("prompt_template") or "")
    if step.get("type") == "retrieve" and isinstance(step.get("retrieve"), dict):
        texts.append(str(step["retrieve"].get("query") or ""))
    from core.templating import STATE_REF, one_form

    # The one form (F1252); a definition not yet saved may still hold the older one, read as it will be saved.
    return {m.group(1).split(".")[0] for text in texts for m in STATE_REF.finditer(one_form(text))}


#: `state.key` in a Transform's code, never a dict method (`state.get(…)`, `state.items`).
_CODE_ATTR = re.compile(r"\bstate\.([A-Za-z_]\w*)\b(?!\s*\()")
#: `state["key"]` / `state['key']`.
_CODE_ITEM = re.compile(r"\bstate\[\s*(['\"])([^'\"]+)\1\s*\]")
_DICT_NAMES = frozenset(dir(dict))


def guarded(code: str, key: str) -> bool:
    """The code asks whether `key` is there before it reads it, or writes it itself first."""
    k = re.escape(key)
    return bool(re.search(rf"(['\"]){k}\1\s+(?:not\s+)?in\s+state\b", code)
                or re.search(rf"hasattr\(\s*state\s*,\s*(['\"]){k}\1", code)
                or re.search(rf"\bstate(?:\.{k}|\[\s*(['\"]){k}\1\s*\])\s*=(?!=)", code))


def hard_reads(step: dict) -> dict[str, str]:
    """The keys a step breaks without, each with what happens then: a Transform's unguarded reads, a Loop's list."""
    if step.get("type") == "transform" and isinstance(step.get("transform_code"), str):
        code = step["transform_code"]
        keys = {m.group(1) for m in _CODE_ATTR.finditer(code) if m.group(1) not in _DICT_NAMES}
        keys |= {m.group(2).split(".")[0] for m in _CODE_ITEM.finditer(code)}
        return {k: "its code stops there (no key '" + k + "')" for k in sorted(keys) if not guarded(code, k)}
    if step.get("type") == "loop" and isinstance(step.get("loop_over"), str):
        from core.templating import STATE_REF, one_form

        expr = one_form(step["loop_over"])
        keys = {m.group(1).split(".")[0] for m in STATE_REF.finditer(expr)}
        keys |= {m.group(1) for m in _CODE_ATTR.finditer(expr) if m.group(1) not in _DICT_NAMES}
        keys |= {m.group(2).split(".")[0] for m in _CODE_ITEM.finditer(expr)}
        return {k: "it loops over nothing there and the run goes on as if it had" for k in sorted(keys)}
    if step.get("type") == "tool" and step.get("tool_arguments") is not None:
        # A Tool step's set arguments (E1) are rendered from state with no model to repair them: a key a run reaches
        # unset stops the run, "could not set its arguments" (F1617: a save after a Switch read each arm's own key).
        from core.templating import STATE_REF

        keys: set[str] = set()

        def walk(value: object) -> None:
            if isinstance(value, str):
                keys.update(m.group(1).split(".")[0] for m in STATE_REF.finditer(value))
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(step["tool_arguments"])
        return {k: "its arguments cannot be set there and the run stops" for k in sorted(keys)}
    return {}


# ── the ways in ───────────────────────────────────────────────────────────────────────────────────────────────────

class Walk:
    """One workflow's ways from its entry, walked once for each key asked about and kept."""

    def __init__(self, orch: dict):
        self.steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")]
        self.by_id = {s["id"]: s for s in self.steps}
        self.entry = orch.get("entry_step_id")
        self._unset: dict[str, tuple[set[str], dict[str, tuple[str, str]]]] = {}

    def unset(self, key: str) -> tuple[set[str], dict[str, tuple[str, str]]]:
        """The steps a run can reach before any step writes `key`, and each one's way in: the step before it and
        which of its ways it took (F1346 names the branch that misses the key).

        Walked from the entry; a step that writes it ends that path (a step reading and writing the key reads it
        first, so it is reached). A Loop or a Parallel leads into its body (a Loop's in order, a Parallel's every
        branch) and on to its own next step only when nothing in it writes the key: a Loop's body is taken to run,
        and every branch of a Parallel runs before it goes on.
        """
        if key not in self._unset:
            self._unset[key] = _first_unwritten(key, self.entry, self.by_id)
        return self._unset[key]


def _first_unwritten(key: str, entry: str | None,
                     by_id: dict[str, dict]) -> tuple[set[str], dict[str, tuple[str, str]]]:
    in_loop: dict[str, str | None] = {}  # a Loop body step -> the one after it
    for step in by_id.values():
        if step.get("type") == "loop":
            body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
            for index, sid in enumerate(body):
                in_loop[sid] = body[index + 1] if index + 1 < len(body) else None
    reached: set[str] = set()
    parents: dict[str, tuple[str, str]] = {}
    queue = [entry] if entry in by_id else []
    while queue:
        sid = queue.pop()
        if sid in reached:
            continue
        reached.add(sid)
        step = by_id[sid]
        if key in own_writes(step):
            continue
        targets: list[tuple[str, str | None]] = []
        if step.get("type") == "loop":
            body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
            targets += [("body", b) for b in body[:1]]
        elif step.get("type") == "parallel":
            targets += [(f"branch {i}", chain[0]) for i, chain in enumerate(branch_chains(step, by_id), 1) if chain]
        container = step.get("type") in ("loop", "parallel")
        if not container or key not in writes(sid, by_id, set()):
            if sid in in_loop:
                targets.append(("next", in_loop[sid]))
            else:
                targets += [("next", step.get("next_step_id")), ("true branch", step.get("if_true_step_id")),
                            ("false branch", step.get("if_false_step_id")),
                            ("default case", step.get("switch_default_step_id")),
                            ("way when its tries run out", step.get("on_exhausted_step_id")),
                            *((f"'{k}' route", t) for k, t in (step.get("route_map") or {}).items()),
                            *((f"'{k}' case", t) for k, t in (step.get("switch_cases") or {}).items())]
        for way, target in targets:
            if target and target in by_id and target not in reached:
                queue.append(target)
                parents.setdefault(target, (sid, way))
    return reached, parents


def way_in(sid: str, parents: dict[str, tuple[str, str]]) -> str:
    """How a run reaches `sid` with the key unset: by the last branch it takes on the way, and the step after it."""
    path, at, seen = [], sid, {sid}
    while at in parents:
        before, way = parents[at]
        path.append((before, way, at))
        if before in seen:
            break
        seen.add(before)
        at = before
    for before, way, after in path:  # nearest first
        if way not in ("next", "body"):
            return f"from '{before}' by its {way}" + (f", through '{after}'" if after != sid else "")
    return "before any step writes it"
