"""Which keys a Parallel's branches write, and which ones two branches share (F256).

Branches share one state, so two that write the same key leave one value — the
later writer's — and a Merge labels it with whichever step it finds first. It
used to pass silently. The engine now fails the Parallel before any branch runs,
and the builder's validator reports it; the editor's `validate.ts` mirrors this.

Plain dicts, so the validator (which holds the stored document) and the engine
(`model_dump` of its steps) share it. It imports nothing of the engine.
"""
from __future__ import annotations

import re


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


def _writes(step_id: str, by_id: dict[str, dict], seen: set[str]) -> set[str]:
    """The keys a step writes: its output key, and a Loop's or a nested Parallel's body."""
    step = by_id.get(step_id)
    if not step or step_id in seen:
        return set()
    seen.add(step_id)
    keys = {step["output_key"]} if step.get("output_key") else set()
    if step.get("type") == "loop":
        for body_id in step.get("loop_step_ids") or []:
            keys |= _writes(body_id, by_id, seen)
    elif step.get("type") == "parallel":
        for chain in branch_chains(step, by_id):
            for sid in chain:
                keys |= _writes(sid, by_id, seen)
    return keys


def shared_branch_keys(parallel: dict, by_id: dict[str, dict]) -> list[str]:
    """One sentence per key that two or more of `parallel`'s branches write."""
    owners: dict[str, dict[int, list[str]]] = {}
    for index, chain in enumerate(branch_chains(parallel, by_id)):
        for sid in chain:
            for key in _writes(sid, by_id, set()):
                owners.setdefault(key, {}).setdefault(index, []).append((by_id.get(sid) or {}).get("name") or sid)
    sentences = []
    for key, by_branch in owners.items():
        if len(by_branch) < 2:
            continue
        numbers = [str(i + 1) for i in sorted(by_branch)]
        which = ", ".join(numbers[:-1]) + " and " + numbers[-1]
        steps = ", ".join(f"'{n}'" for i in sorted(by_branch) for n in by_branch[i])
        verb = "both write" if len(numbers) == 2 else "all write"
        sentences.append(
            f"Branches {which} of '{parallel.get('name') or parallel.get('id')}' {verb} '{key}' "
            f"({steps}) — only one value would survive. Give each branch its own output key."
        )
    return sentences


def same_key_branches(orch: dict) -> list[str]:
    """`shared_branch_keys` for every Parallel in an orchestration document."""
    steps = orch.get("steps") or []
    by_id = {s.get("id"): s for s in steps if s.get("id")}
    found: list[str] = []
    for step in steps:
        if step.get("type") == "parallel":
            found += shared_branch_keys(step, by_id)
    return found


#: The fields a step reads shared state through: `{{ state.x }}` in a template,
#: `state.x`, `state["x"]` or `state.get("x")` in an expression or a transform.
_READ_FIELDS = ("prompt_template", "evaluator_prompt", "print_content", "transform_code",
                "if_condition", "switch_expression", "loop_over", "loop_while_condition",
                "loop_break_condition")
_STATE_REF = re.compile(
    r"\bstate\s*(?:\.get\(\s*['\"]([^'\"]+)['\"]|\[\s*['\"]([^'\"]+)['\"]\s*\]|\.\s*([A-Za-z_]\w*))")


def _reads(step_id: str, by_id: dict[str, dict], seen: set[str]) -> set[str]:
    """The keys a step names as its input: `input_keys`, and every state reference
    in its templates and expressions — a Loop's or a nested Parallel's body too.
    Generous by design: a name that is not really a read only keeps an order."""
    step = by_id.get(step_id)
    if not step or step_id in seen:
        return set()
    seen.add(step_id)
    keys = {k for k in step.get("input_keys") or [] if k}
    for field in _READ_FIELDS:
        for match in _STATE_REF.finditer(step.get(field) or ""):
            keys.add(next(g for g in match.groups() if g))
    if step.get("type") == "loop":
        for body_id in step.get("loop_step_ids") or []:
            keys |= _reads(body_id, by_id, seen)
    elif step.get("type") == "parallel":
        for chain in branch_chains(step, by_id):
            for sid in chain:
                keys |= _reads(sid, by_id, seen)
    return keys


def sibling_reads(parallel: dict, by_id: dict[str, dict]) -> dict[int, list[str]]:
    """Per branch, the keys it reads that another of `parallel`'s branches writes.

    Branches that run one after another have always let a later branch read an
    earlier one's output. Run at the same time, the reader would see the key
    either way depending on which finished first — so such a branch keeps its
    place in order (F255).
    """
    chains = branch_chains(parallel, by_id)
    writes = [set().union(*(_writes(sid, by_id, set()) for sid in chain)) if chain else set()
              for chain in chains]
    found: dict[int, list[str]] = {}
    for index, chain in enumerate(chains):
        reads = set().union(*(_reads(sid, by_id, set()) for sid in chain)) if chain else set()
        others = set().union(*(w for i, w in enumerate(writes) if i != index)) if len(writes) > 1 else set()
        shared = sorted(reads & others)
        if shared:
            found[index] = shared
    return found


#: What every Loop writes as it goes, whatever its keys are called — a body's
#: template can read them (`{{ state._loop_current_iteration }}`).
LOOP_MARKERS = ("_loop_current_iteration", "_loop_total")


def _loop_keys(step_id: str, by_id: dict[str, dict], seen: set[str]) -> set[str]:
    """The keys every Loop in a step's reach writes each iteration: its index
    key, and a for-each's item key. Not in `_writes`: two loops in two branches
    that keep the default `loop_index` have always worked, one after the other,
    and must not be refused (F256) — they keep their order instead (F301)."""
    step = by_id.get(step_id)
    if not step or step_id in seen:
        return set()
    seen.add(step_id)
    keys: set[str] = set()
    if step.get("type") == "loop":
        keys.add(step.get("loop_index_key") or "loop_index")
        if step.get("loop_over"):
            keys.add(step.get("loop_item_key") or "loop_item")
        for body_id in step.get("loop_step_ids") or []:
            keys |= _loop_keys(body_id, by_id, seen)
    elif step.get("type") == "parallel":
        for chain in branch_chains(step, by_id):
            for sid in chain:
                keys |= _loop_keys(sid, by_id, seen)
    return keys


def loop_key_conflicts(parallel: dict, by_id: dict[str, dict]) -> dict[int, list[str]]:
    """Per branch, the loop keys it would share with another branch if the two
    ran at the same time (F301): a key its loop writes that another branch's
    loop or output writes too, a key another branch's loop writes that it reads,
    and — beside any other loop — the iteration markers, if it reads them. Such
    a branch keeps its place in order, as a reader of a sibling's output does."""
    chains = branch_chains(parallel, by_id)

    def _union(fn, chain):
        return set().union(*(fn(sid, by_id, set()) for sid in chain)) if chain else set()

    loops = [_union(_loop_keys, c) for c in chains]
    writes = [_union(_writes, c) for c in chains]
    reads = [_union(_reads, c) for c in chains]
    found: dict[int, list[str]] = {}
    for index in range(len(chains)):
        others = [j for j in range(len(chains)) if j != index]
        their_loops = set().union(*(loops[j] for j in others)) if others else set()
        their_writes = set().union(*(writes[j] for j in others)) if others else set()
        shared = (loops[index] & (their_loops | their_writes)) | (reads[index] & their_loops)
        if their_loops:
            shared |= reads[index] & set(LOOP_MARKERS)
        if shared:
            found[index] = sorted(shared)
    return found


# ── A key read before any step writes it (U74) ──────────────────────────────

#: The keys the engine writes before any step: the run's input, and a workflow
#: agent's conversation (`run_orchestrator_agent`).
ENGINE_KEYS = frozenset({"user_input", "user_query", "history"})



def _prompt_reads(step: dict) -> set[str]:
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


def _own_writes(step: dict) -> set[str]:
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


def _first_unwritten(key: str, entry: str | None, by_id: dict[str, dict],
                     parents: dict[str, tuple[str, str]] | None = None) -> set[str]:
    """The steps a run can reach before any step writes `key`.

    Walked from the entry; a step that writes it ends that path (a step reading
    and writing the key reads it first, so it is reached). A Loop or a Parallel
    leads into its body — a Loop's in order, a Parallel's every branch — and on
    to its own next step only when nothing in it writes the key: a Loop's body
    is taken to run, and every branch of a Parallel runs before it goes on.
    `parents`, when given, gets each reached step's way in: the step before it
    and which of its ways it took (F1346 names the branch that misses a key).
    """
    in_loop: dict[str, str | None] = {}  # a Loop body step -> the one after it
    for step in by_id.values():
        if step.get("type") == "loop":
            body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
            for index, sid in enumerate(body):
                in_loop[sid] = body[index + 1] if index + 1 < len(body) else None
    reached: set[str] = set()
    queue = [entry] if entry in by_id else []
    while queue:
        sid = queue.pop()
        if sid in reached:
            continue
        reached.add(sid)
        step = by_id[sid]
        if key in _own_writes(step):
            continue
        targets: list[tuple[str, str | None]] = []
        if step.get("type") == "loop":
            body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
            targets += [("body", b) for b in body[:1]]
        elif step.get("type") == "parallel":
            targets += [(f"branch {i}", chain[0]) for i, chain in enumerate(branch_chains(step, by_id), 1) if chain]
        container = step.get("type") in ("loop", "parallel")
        if not container or key not in _writes(sid, by_id, set()):
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
                if parents is not None:
                    parents.setdefault(target, (sid, way))
    return reached


def unset_reads(orch: dict) -> list[str]:
    """One sentence per step whose prompt reads a key before any step writes it.

    A warning, never a refusal: a feedback loop's first pass is a real pattern,
    and the key now reads empty there (U74). A key the schema declares is filled
    by its default or the run's caller, and the engine's own keys always are.
    """
    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    by_id = {s["id"]: s for s in steps}
    declared = set(orch.get("state_schema") or {})
    written = set().union(*(_own_writes(s) for s in steps)) if steps else set()
    sentences: list[str] = []
    walks: dict[str, set[str]] = {}
    for step in steps:
        for key in sorted(_prompt_reads(step) - ENGINE_KEYS - declared):
            if key not in written:
                sentences.append(
                    f"Step '{step['id']}' reads {{{{ state.{key} }}}}, which no step writes, so it always reads "
                    f"empty. Write it in an earlier step, or give it a default in the state schema.")
                continue
            if key not in walks:
                walks[key] = _first_unwritten(key, orch.get("entry_step_id"), by_id)
            if step["id"] in walks[key]:
                sentences.append(
                    f"Step '{step['id']}' reads {{{{ state.{key} }}}} before any step writes it, so its first pass "
                    f"reads it empty. Give '{key}' a default in the state schema if it should read something.")
    return sentences


# ── reads that break without the key (F1346) ──────────────────────────────────────────────────────────────────────
# cap-05: a Transform read `state.extract_webhook` after a join only one branch of which wrote it, and the run that
# took the other failed "no key 'extract_webhook' here". A prompt reads an unset key empty (above, a warning); these
# two readers break: a Transform's state has no default for a missing key (`execution.scripts`), and a Loop over a
# missing list runs no iteration while the run reads Completed.

#: `state.key` in a Transform's code, never a dict method (`state.get(…)`, `state.items`).
_CODE_ATTR = re.compile(r"\bstate\.([A-Za-z_]\w*)\b(?!\s*\()")
#: `state["key"]` / `state['key']`.
_CODE_ITEM = re.compile(r"\bstate\[\s*(['\"])([^'\"]+)\1\s*\]")
_DICT_NAMES = frozenset(dir(dict))


def _guarded(code: str, key: str) -> bool:
    """The code asks whether `key` is there before it reads it, or writes it itself first."""
    k = re.escape(key)
    return bool(re.search(rf"(['\"]){k}\1\s+(?:not\s+)?in\s+state\b", code)
                or re.search(rf"hasattr\(\s*state\s*,\s*(['\"]){k}\1", code)
                or re.search(rf"\bstate(?:\.{k}|\[\s*(['\"]){k}\1\s*\])\s*=(?!=)", code))


def _hard_reads(step: dict) -> dict[str, str]:
    """The keys a step breaks without, each with what happens then: a Transform's unguarded reads, a Loop's list."""
    if step.get("type") == "transform" and isinstance(step.get("transform_code"), str):
        code = step["transform_code"]
        keys = {m.group(1) for m in _CODE_ATTR.finditer(code) if m.group(1) not in _DICT_NAMES}
        keys |= {m.group(2).split(".")[0] for m in _CODE_ITEM.finditer(code)}
        return {k: "its code stops there (no key '" + k + "')" for k in sorted(keys) if not _guarded(code, k)}
    if step.get("type") == "loop" and isinstance(step.get("loop_over"), str):
        from core.templating import STATE_REF, one_form

        expr = one_form(step["loop_over"])
        keys = {m.group(1).split(".")[0] for m in STATE_REF.finditer(expr)}
        keys |= {m.group(1) for m in _CODE_ATTR.finditer(expr) if m.group(1) not in _DICT_NAMES}
        keys |= {m.group(2).split(".")[0] for m in _CODE_ITEM.finditer(expr)}
        return {k: "it loops over nothing there and the run goes on as if it had" for k in sorted(keys)}
    return {}


def _way_in(sid: str, parents: dict[str, tuple[str, str]]) -> str:
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


def missing_key_reads(orch: dict) -> list[str]:
    """One sentence per step a run can reach without a key it breaks without (F1346): its name, the key, the branch
    that misses it, and the three ways to fix it. Not a key with a default, the run's own input (declared and
    written by no step, so filled when it starts), or the engine's own."""
    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    by_id = {s["id"]: s for s in steps}
    schema = orch.get("state_schema") or {}
    written = set().union(*(_own_writes(s) for s in steps)) if steps else set()
    filled = {k for k, v in schema.items() if isinstance(v, dict) and "default" in v} | (set(schema) - written)
    sentences: list[str] = []
    for step in steps:
        for key, breaks in _hard_reads(step).items():
            if key in ENGINE_KEYS or key in filled:
                continue
            fixes = (f"read it with state.get('{key}'), or give it a default in the state schema"
                     if step.get("type") == "transform" else
                     "an empty list where there is nothing, or give it a default of [] in the state schema")
            if key not in written:
                sentences.append(f"Step '{step['id']}' reads state.{key}, which no step writes, so {breaks}. Write "
                                 f"it in an earlier step, {fixes}.")
                continue
            parents: dict[str, tuple[str, str]] = {}
            if step["id"] in _first_unwritten(key, orch.get("entry_step_id"), by_id, parents):
                way = _way_in(step["id"], parents)
                reached = (f"a run can reach it {way}" if way.startswith("before") else
                           f"a run reaches it {way}, without '{key}' set")
                sentences.append(f"Step '{step['id']}' reads state.{key}, but {reached}, so {breaks}. Write it on "
                                 f"that way too, {fixes}.")
    return sentences

