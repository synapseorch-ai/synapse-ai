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

from core.orchestration.state_reads import (ENGINE_KEYS, Walk, branch_chains, hard_reads, own_writes, prompt_reads,
                                            way_in)
from core.orchestration.state_reads import writes as _writes


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


# ── A key read before any step writes it (U74), and one a step breaks without (F1346) ─────────────────────────────
# The reads, the writes and the ways in are `state_reads`' (F1621); these say what each finds.


def unset_reads(orch: dict) -> list[str]:
    """One sentence per step whose prompt reads a key before any step writes it.

    A warning, never a refusal: a feedback loop's first pass is a real pattern,
    and the key now reads empty there (U74). A key the schema declares is filled
    by its default or the run's caller, and the engine's own keys always are.
    """
    walk = Walk(orch)
    declared = set(orch.get("state_schema") or {})
    written = _written(walk)
    sentences: list[str] = []
    for step in walk.steps:
        for key in sorted(prompt_reads(step) - ENGINE_KEYS - declared):
            if key not in written:
                sentences.append(
                    f"Step '{step['id']}' reads {{{{ state.{key} }}}}, which no step writes, so it always reads "
                    f"empty. Write it in an earlier step, or give it a default in the state schema.")
                continue
            if step["id"] in walk.unset(key)[0]:
                sentences.append(
                    f"Step '{step['id']}' reads {{{{ state.{key} }}}} before any step writes it, so its first pass "
                    f"reads it empty. Give '{key}' a default in the state schema if it should read something.")
    return sentences


# cap-05: a Transform read `state.extract_webhook` after a join only one branch of which wrote it, and the run that
# took the other failed "no key 'extract_webhook' here". A prompt reads an unset key empty (above, a warning); these
# readers break: a Transform's state has no default for a missing key (`execution.scripts`), a Loop over a missing
# list runs no iteration while the run reads Completed, and a Tool step's set arguments cannot be set (F1617).


def _written(walk: Walk) -> set[str]:
    return set().union(*(own_writes(s) for s in walk.steps)) if walk.steps else set()


def missing_key_reads(orch: dict) -> list[str]:
    """One sentence per step a run can reach without a key it breaks without (F1346): its name, the key, the branch
    that misses it, and the three ways to fix it. Not a key with a default, the run's own input (declared and
    written by no step, so filled when it starts), or the engine's own."""
    walk = Walk(orch)
    schema = orch.get("state_schema") or {}
    written = _written(walk)
    filled = {k for k, v in schema.items() if isinstance(v, dict) and "default" in v} | (set(schema) - written)
    sentences: list[str] = []
    for step in walk.steps:
        for key, breaks in hard_reads(step).items():
            if key in ENGINE_KEYS or key in filled:
                continue
            fixes = (f"read it with state.get('{key}'), or give it a default in the state schema"
                     if step.get("type") == "transform" else
                     "or write one key every arm writes (each arm's step answering the same output_key) and read "
                     "that, or do this step in each arm"
                     if step.get("type") == "tool" else
                     "an empty list where there is nothing, or give it a default of [] in the state schema")
            if key not in written:
                if step.get("type") == "tool":
                    continue  # a key no step writes: the validator's own Tool-arguments check says it (E1)
                sentences.append(f"Step '{step['id']}' reads state.{key}, which no step writes, so {breaks}. Write "
                                 f"it in an earlier step, {fixes}.")
                continue
            reached, parents = walk.unset(key)
            if step["id"] in reached:
                way = way_in(step["id"], parents)
                reached_by = (f"a run can reach it {way}" if way.startswith("before") else
                              f"a run reaches it {way}, without '{key}' set")
                sentences.append(f"Step '{step['id']}' reads state.{key}, but {reached_by}, so {breaks}. Write it on "
                                 f"that way too, {fixes}.")
    return sentences
