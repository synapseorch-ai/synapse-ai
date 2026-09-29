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


#: The fields a step reads shared state through: `{state.x}` in a template,
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
