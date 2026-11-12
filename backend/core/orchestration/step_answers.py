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
    """A Human step's fields, as stored or as the builder's tools send them (`human_fields_json`)."""
    fields = step.get("human_fields")
    if fields is None and isinstance(step.get("human_fields_json"), str):
        try:
            fields = json.loads(step["human_fields_json"] or "[]")
        except ValueError:
            fields = []
    return [f for f in fields or [] if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]]


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
                f"Approve and Decline when it is the step's one yes/no; \"textarea\" for their words — or read what "
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
