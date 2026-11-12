"""What a step's answer holds, where the step alone says it (F1549, BLD3; MIG1's bug A).

A step writes its answer to its `output_key`, and for a few types the step itself says what that answer is, so a field
a later step reads out of it can be checked before any run (`builder_tools._validate_orchestration`):

* a **Human** step: its fields by name (`human_fields`, F455; a yes/no said in words is the flag, F1434). With no
  fields, the person's words (`engine._as_answered`, F106: the run page and a channel send the text; the docs say the
  same), which have no fields. Every Human step also writes `human_response` (`engine.resume_paused`);
* an **Evaluator**: the label of the route it took (`EvaluatorStepExecutor`);
* an **If/Else**: true or false (`IfElseStepExecutor`);
* a **Retrieve** or an **Ingest** step: its contract's output (`RetrieveStepOutput`, `IngestStepOutput`);
* an **LLM** step: the fields it declares (`output_fields`, F1553), which the run holds its model to; with none, its
  text, which has no fields;
* an **Agent** step: the fields it declares; with none, its text when its agent is known to be a plain one (not a
  workflow) and it finishes with no tool (E2's finish is that tool's object).

Any other step's answer is not known from the step: a tool's text (JSON or not, F801), a Transform's or an Extract
JSON step's data, a Switch's value, a Merge's, a Loop's, a called workflow's, a Browser step's, an Agent step's whose
agent is not known. A Tool step's is known only from the workspace's tools, and is F1394's
(`build_plan._fields_its_answer_lacks`, said once).
"""
from __future__ import annotations

import json
from typing import Any

#: The key every Human step's answer is written to as well, and the one it is written to when it names none.
HUMAN_RESPONSE = "human_response"


def human_fields(step: dict[str, Any]) -> list[dict[str, Any]]:
    """A Human step's fields, as stored or as the builder's tools send them (`human_fields_json`); a field named by
    `key`, as the template packs wrote one, by its key (F1557: `human_fields.named`)."""
    from core.orchestration.human_fields import named

    fields = step.get("human_fields")
    if fields is None and isinstance(step.get("human_fields_json"), str):
        try:
            fields = json.loads(step["human_fields_json"] or "[]")
        except ValueError:
            fields = []
    fields = named(fields) if isinstance(fields, list) else []
    return [f for f in fields if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]]


def declared_fields(step: dict[str, Any]) -> list[dict[str, Any]]:
    """A model step's declared answer fields (F1553), as stored or as the builder's tools send them."""
    from core.orchestration.output_fields import normalised

    fields = step.get("output_fields")
    if not fields and isinstance(step.get("output_fields_json"), str):
        fields = step["output_fields_json"]
    return normalised(fields)


def answer_fields(step: dict[str, Any], agents: dict | None = None) -> tuple[str, ...] | None:
    """Every field the step's answer has, `()` for an answer with none, or None when the step does not say.
    `agents` (by id), when known, say whether an Agent step's agent answers in words."""
    kind = step.get("type")
    if kind in ("llm", "agent") and (declared := declared_fields(step)):
        return tuple(f["name"].strip() for f in declared)
    if kind == "llm":
        return ()
    if kind == "agent":
        agent = (agents or {}).get(step.get("agent_id")) if agents is not None else None
        if agent is None or step.get("finish_tools") or agent.get("type") == "orchestrator":
            return None
        return ()
    if kind == "human":
        return tuple(f["name"] for f in human_fields(step))
    if kind in ("evaluator", "if_else"):
        return ()
    if kind in ("retrieve", "ingest"):
        from core.knowledge.api_models import IngestStepOutput, RetrieveStepOutput

        return tuple((RetrieveStepOutput if kind == "retrieve" else IngestStepOutput).model_fields)
    return None


def keys_written(step: dict[str, Any]) -> list[str]:
    """The state keys the step writes its answer to (a Human step's to `human_response` too)."""
    key = step.get("output_key") or (HUMAN_RESPONSE if step.get("type") == "human" else None)
    keys = [key] if key else []
    if step.get("type") == "human" and HUMAN_RESPONSE not in keys:
        keys.append(HUMAN_RESPONSE)
    return keys


def _ticked(names: Any) -> str:
    return ", ".join(f"`{n}`" for n in names)


def declare(step: dict[str, Any], field: str) -> str:
    """The repair for a read of a field a model step does not declare (F1553): declare it, by its type."""
    sid, has = step.get("id", "?"), [f["name"].strip() for f in declared_fields(step)]
    also = f", or read one it declares ({_ticked(has)})" if has else f", or read state.{step.get('output_key') or sid} " \
                                                                      "itself, its text"
    return (f"declare `{field}` in '{sid}'s output_fields — "
            f'{{"name": "{field}", "type": "boolean"}} for a yes or no, "choice" with "options" for one of a few '
            'words, "number", "list" or "text" — and the run holds its model to them' + also)


def repair(step: dict[str, Any], key: str, field: str) -> str:
    """What a reader of `field` in `key`, which `step` never writes, does instead: the repair first."""
    sid, kind, has = step.get("id", "?"), step.get("type"), answer_fields(step) or ()
    if kind in ("llm", "agent"):
        return declare(step, field)
    if kind == "human" and not has:
        return (f"give '{sid}' a field named '{field}' in human_fields — "
                f'{{"name": "{field}", "type": "boolean", "label": "…?"}} for a yes or no, which the run asks with '
                f"Approve and Decline when it is the step's one approval (a yes/no named like an approval, `approved`, "
                f"or marked \"approval\": true); \"textarea\" for their words — or read what "
                f"'{sid}' writes. With no fields, its answer is the person's words, which have no fields")
    if kind == "human":
        return f"read one of its fields ({_ticked(has)}), or add a field named '{field}' to its human_fields"
    if kind == "evaluator":
        labels = list((step.get("route_map") or {}))
        return f"read state.{key} itself, the label of the route it took" + (f" ({_ticked(labels)})" if labels else "")
    if kind == "if_else":
        return f"read state.{key} itself, true or false"
    return f"read one of its fields ({_ticked(has)})"


def kind_words(step: dict[str, Any]) -> str:
    return {"if_else": "if/else"}.get(step.get("type") or "", step.get("type") or "")


# ── A decision on a typed answer that cannot follow it (F1558) ────────────────────────────────────────────────────────

#: The conditions that decide on a value's truth, and every condition that reads one (a Switch's expression too).
TRUTH_FIELDS = ("if_condition", "loop_while_condition", "loop_break_condition")
CONDITION_FIELDS = (*TRUTH_FIELDS, "switch_expression")


def _typed(step: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The fields a step declares, each {name, kind, options}, or None when it declares none: a Human step's fields, a
    model step's answer fields. `kind` is the field's type ('boolean', 'choice', …), None for one not known."""
    from core.orchestration import human_fields as _human
    from core.orchestration import output_fields as _answer

    if step.get("type") == "human":
        fields, kind_of = human_fields(step), _human.field_type
    elif step.get("type") in ("llm", "agent"):
        fields, kind_of = declared_fields(step), _answer.field_type
    else:
        return None
    if not fields:
        return None
    return [{"name": f["name"].strip(), "kind": kind_of(f.get("type")), "options": _human.field_options(f)}
            for f in fields]


def fielded(steps: list) -> dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    """Each key every step writing which declares its fields: (its first writer, {field: {kind, options}}). A field's
    kind is kept only where every writer that has the field gives it one (a choice's options are each one's)."""
    writers: dict[str, list[dict]] = {}
    for s in steps:
        if isinstance(s, dict):
            for key in keys_written(s):
                writers.setdefault(key, []).append(s)
    out: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for key, ws in writers.items():
        typed = [_typed(w) for w in ws]
        if any(t is None for t in typed):
            continue
        fields: dict[str, dict[str, Any]] = {}
        for declared in typed:
            for f in declared or []:
                had = fields.setdefault(f["name"], {"kind": f["kind"], "options": []})
                if had["kind"] != f["kind"]:
                    had["kind"] = None
                had["options"] += [o for o in f["options"] if o not in had["options"]]
        out[key] = (ws[0], fields)
    return out


def _whole_key(node: Any) -> str | None:
    """The key a node reads whole: `state.k`, `state['k']`."""
    import ast

    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "state":
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "state" \
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _field_read(node: Any) -> tuple[str, str] | None:
    """(key, field) a node reads: `state.k.f`, `state.k['f']`, `state['k'].f`, `state['k']['f']`."""
    import ast

    if isinstance(node, ast.Attribute) and (key := _whole_key(node.value)):
        return key, node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str) \
            and (key := _whole_key(node.value)):
        return key, node.slice.value
    return None


def _quoted(words: Any) -> str:
    return ", ".join(f"'{w}'" for w in words)


_RUNS = " It runs as it is today."


def _never_said(sid: str, tree: Any, known: dict, truth: bool) -> list[str]:
    import ast

    said: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            for read, other in ((operands[i], operands[i + 1]), (operands[i + 1], operands[i])):
                ref = _field_read(read)
                if not ref or not (isinstance(other, ast.Constant) and isinstance(other.value, str)):
                    continue
                key, name = ref
                field = (known.get(key) or (None, {}))[1].get(name)
                word = other.value
                if not field:
                    continue
                if field["kind"] == "boolean":
                    said.append(f"Step '{sid}' compares state.{key}.{name}, a yes/no, with '{word}': it holds true or "
                                f"false, so the comparison never follows the answer — compare it with True or False."
                                + _RUNS)
                elif field["kind"] == "choice" and field["options"] and word not in field["options"]:
                    near = [o for o in field["options"] if o.strip().lower() == word.strip().lower()]
                    fix = f"use '{near[0]}'" if near else "compare it with one of them"
                    said.append(f"Step '{sid}' compares state.{key}.{name}, one of {_quoted(field['options'])}, with "
                                f"'{word}': the answer is the option as written, so the comparison never follows it — "
                                f"{fix}." + _RUNS)
    if truth:
        top = tree.body.operand if isinstance(tree.body, ast.UnaryOp) and isinstance(tree.body.op, ast.Not) \
            else tree.body
        key = _whole_key(top)
        if key is None and isinstance(top, ast.Compare) and len(top.ops) == 1 \
                and isinstance(top.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            for read, other in ((top.left, top.comparators[0]), (top.comparators[0], top.left)):
                if isinstance(other, ast.Constant) and isinstance(other.value, bool) and _whole_key(read):
                    key = _whole_key(read)
        if key in known:
            writer, fields = known[key]
            names = list(fields)
            first = next((n for n in names if fields[n]["kind"] == "boolean"), names[0] if names else "<field>")
            said.append(f"Step '{sid}' decides on state.{key} whole, {kind_words(writer)} step "
                        f"'{writer.get('id', '?')}''s answer: a mapping of its fields, never empty and never True "
                        f"itself, so this never follows it — read one of its fields, e.g. state.{key}.{first}." + _RUNS)
    return said


def never_followed(steps: list) -> list[str]:
    """A condition on a typed answer that cannot follow it (F1558): a yes/no compared with a word (it holds true or
    false, F1434), a choice compared with a word that is not one of its options (it holds the option as written, F455),
    and, where a condition decides on a value's truth, the whole answer of fields (a mapping, never empty). Warnings,
    the fix in each: the run is unchanged. The editor says the same (`humanFields.answerValueChecks`)."""
    import ast

    from core.expressions import _unbraced

    known = fielded(steps)
    if not known:
        return []
    out: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        for name in CONDITION_FIELDS:
            expr = s.get(name)
            if not isinstance(expr, str) or not expr.strip():
                continue
            try:
                tree = ast.parse(_unbraced(expr), mode="eval")
            except SyntaxError:
                continue
            for said in _never_said(s.get("id", "?"), tree, known, truth=name in TRUTH_FIELDS):
                if said not in out:
                    out.append(said)
    return out
