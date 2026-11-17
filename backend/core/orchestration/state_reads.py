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


def read_by_another(key: str | None, steps: object, *, but: str) -> bool:
    """Whether a step other than `but` reads `key` (F1677): by its input keys, a `{{ state.key }}` in its prompt, or a
    Transform's code or a Loop's list."""
    if not key:
        return False
    for step in steps or []:
        d = step.model_dump(mode="json") if hasattr(step, "model_dump") else step
        if not isinstance(d, dict) or d.get("id") == but:
            continue
        if key in (d.get("input_keys") or []) or key in prompt_reads(d) or key in hard_reads(d):
            return True
    return False


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
    if step.get("type") == "ingest" and isinstance(step.get("ingest"), dict):
        # An Ingest step's records come from one key: with it unset, the step fails "nothing to ingest" (F1621).
        source = str(step["ingest"].get("records_from") or "").strip()
        source = source[len("state."):] if source.startswith("state.") else source
        return {source: "it has nothing to ingest there and the step fails"} if source else {}
    return {}


# ── the ways in ───────────────────────────────────────────────────────────────────────────────────────────────────

class Walk:
    """One workflow's ways from its entry, walked once for each key asked about and kept."""

    def __init__(self, orch: dict):
        self.steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")]
        self.by_id = {s["id"]: s for s in self.steps}
        self.entry = orch.get("entry_step_id")
        self._unset: dict[str, tuple[set[str], dict[str, tuple[str, str]]]] = {}
        self._reaching: dict[str, dict[str, set[str]]] = {}
        self._shape: tuple[dict[str, list[str]], dict[str, tuple[str | None, str]], dict[str, set[str]]] | None = None

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

    def reaching(self, key: str) -> dict[str, set[str]]:
        """For each step, the steps that wrote `key` and whose value a run can bring to it (F1621 part 2): from each
        writer, every way on, as the engine runs it, to the next step that writes the key again (which reads it before
        it writes, so it is reached too). A Loop's body runs in order and round again. A Parallel runs every branch
        before it goes on: a value written in a branch goes on to the join, and one written before the Parallel goes
        on past it only when no branch writes the key again."""
        if key not in self._reaching:
            found: dict[str, set[str]] = {}
            for writer in self.steps:
                if key not in own_writes(writer):
                    continue
                seen: set[str] = set()
                queue = list(self._on(writer["id"], key, writer["id"]))
                while queue:
                    sid = queue.pop()
                    if sid in seen:
                        continue
                    seen.add(sid)
                    found.setdefault(sid, set()).add(writer["id"])
                    if key not in own_writes(self.by_id[sid]):
                        queue.extend(self._on(sid, key, writer["id"]))
            self._reaching[key] = found
        return self._reaching[key]

    def judged_by(self, sid: str, key: str, candidates: list[dict]) -> list[dict]:
        """The writers of `key` (`candidates`, in order) a read in `sid` is judged by: those a run can bring to it, or,
        when none can (no entry to walk from, or a read before every writer), all of them, as every read was before."""
        if self.entry not in self.by_id:
            return candidates
        reach = self.reaching(key).get(sid)
        picked = [w for w in candidates if w.get("id") in reach] if reach else []
        return picked or candidates

    def _on(self, sid: str, key: str, origin: str) -> list[str]:
        """Where a run goes once `sid` is done, carrying `origin`'s value of `key`."""
        body_next, branch_next, members = self._structure()
        step, by_id = self.by_id[sid], self.by_id
        if sid in body_next:
            targets = body_next[sid]
        elif step.get("type") == "loop":
            targets = [b for b in step.get("loop_step_ids") or [] if b in by_id][:1] or [step.get("next_step_id")]
        elif step.get("type") == "parallel":
            targets = [chain[0] for chain in branch_chains(step, by_id) if chain]
            if key not in writes(sid, by_id, set()):
                targets.append(step.get("next_step_id"))
        else:
            nxt = step.get("next_step_id")
            if sid in branch_next:
                nxt, parallel = branch_next[sid]
                if nxt is None and origin in members.get(parallel, ()):
                    nxt = by_id[parallel].get("next_step_id")
            targets = [nxt, step.get("if_true_step_id"), step.get("if_false_step_id"),
                       step.get("switch_default_step_id"), step.get("on_exhausted_step_id"),
                       *(step.get("route_map") or {}).values(), *(step.get("switch_cases") or {}).values()]
        return [t for t in dict.fromkeys(targets) if t and t in by_id]

    def _structure(self) -> tuple[dict[str, list[str]], dict[str, tuple[str | None, str]], dict[str, set[str]]]:
        """A Loop body step's ways on (the next body step, or round again and on), a branch step's next within its
        branch and its Parallel, and every step inside each Parallel's branches."""
        if self._shape is None:
            by_id = self.by_id
            body_next: dict[str, list[str]] = {}
            branch_next: dict[str, tuple[str | None, str]] = {}
            members: dict[str, set[str]] = {}
            for step in self.steps:
                if step.get("type") == "loop":
                    body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
                    for index, sid in enumerate(body):
                        body_next[sid] = ([body[index + 1]] if index + 1 < len(body)
                                          else [body[0], *([step["next_step_id"]] if step.get("next_step_id") else [])])
                elif step.get("type") == "parallel":
                    inside: set[str] = set()
                    for chain in branch_chains(step, by_id):
                        chain = [c for c in chain if c in by_id]
                        for index, sid in enumerate(chain):
                            branch_next.setdefault(sid, (chain[index + 1] if index + 1 < len(chain) else None,
                                                         step["id"]))
                            inside |= _contained(sid, by_id, set())
                    members[step["id"]] = inside
            self._shape = (body_next, branch_next, members)
        return self._shape


def _contained(step_id: str, by_id: dict[str, dict], seen: set[str]) -> set[str]:
    """A step and every step inside it: a Loop's body, a Parallel's branches."""
    step = by_id.get(step_id)
    if not step or step_id in seen:
        return set()
    seen.add(step_id)
    found = {step_id}
    if step.get("type") == "loop":
        for body_id in step.get("loop_step_ids") or []:
            found |= _contained(body_id, by_id, seen)
    elif step.get("type") == "parallel":
        for chain in branch_chains(step, by_id):
            for sid in chain:
                found |= _contained(sid, by_id, seen)
    return found


def _first_unwritten(key: str, entry: str | None,
                     by_id: dict[str, dict]) -> tuple[set[str], dict[str, tuple[str, str]]]:
    in_loop: dict[str, str | None] = {}  # a Loop body step -> the one after it
    in_branch: dict[str, str | None] = {}  # a Parallel's branch step -> the one after it in its branch
    for step in by_id.values():
        if step.get("type") == "loop":
            body = [b for b in step.get("loop_step_ids") or [] if b in by_id]
            for index, sid in enumerate(body):
                in_loop[sid] = body[index + 1] if index + 1 < len(body) else None
        elif step.get("type") == "parallel":
            # Its branches run before it goes on, so a branch step never goes on to the join (F1621): the Parallel
            # does, once every branch has run.
            for chain in branch_chains(step, by_id):
                for index, sid in enumerate(chain):
                    in_branch.setdefault(sid, chain[index + 1] if index + 1 < len(chain) else None)
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
                targets += [("next", in_branch[sid] if sid in in_branch else step.get("next_step_id")),
                            ("true branch", step.get("if_true_step_id")),
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
