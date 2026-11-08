"""What waits on a person, as a product tool (MCP2, D292; F696).

`list_waiting` is the bell's open questions: a workflow run's question (answered with `answer_run`) and a tool call
an agent stopped on, in a scheduled run or an API or phone conversation (answered with `answer_tool_approval`).
Each item names the tool that answers it and the ids to pass. The answers themselves are the run entries
(`runs.py`), an approver's acts.

Where the list comes from is a host's: this product's bell by default (`core.notifications.hub`); cloud's bell is
its own (`set_waiting_source`).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.product_tools import page
from core.product_tools.registry import ProductTool

Source = Callable[[], Awaitable[list[dict]]]

_source: Source | None = None


def set_waiting_source(source: Source | None) -> None:
    """A host's own list of what waits (newest first, in `_item`'s shape); None restores the bell's."""
    global _source
    _source = source


_DATA_KEYS = ("question_id", "approval_id", "member_name", "tool_name", "tool_args", "category", "expires_at",
              "step_id")


def _item(n: dict) -> dict | None:
    data = n.get("data") or {}
    base = {"title": n.get("title"), "detail": n.get("body") or None, "asked_at": n.get("ts"),
            **{k: data[k] for k in _DATA_KEYS if data.get(k)}}
    if n.get("kind") == "human_input" and n.get("run_id"):
        return {"kind": "run_question", "run_id": n["run_id"], **base, "answer_with": "answer_run"}
    if n.get("kind") == "tool_approval":
        where = ({"schedule_id": data["schedule_id"]} if data.get("schedule_id")
                 else {"session_id": data["session_id"]} if data.get("session_id") else None)
        if where is None:
            return None
        return {"kind": "tool_approval", **where, **base, "answer_with": "answer_tool_approval"}
    return None


async def _from_the_bell() -> list[dict]:
    from core.notifications import hub

    items = [_item(n) for n in reversed(hub.list()) if not n.get("resolved")]
    return [i for i in items if i is not None]


async def _list_waiting(caller: Any, args: dict[str, Any]) -> dict:
    items = await (_source or _from_the_bell)()
    if args.get("kind"):
        items = [i for i in items if i.get("kind") == args["kind"]]
    return page.page(items, args)


def entries() -> list[ProductTool]:
    return [ProductTool(
        name="list_waiting",
        description=(
            "What waits on a person in this workspace, newest first: workflow runs asking a question "
            "(answer with answer_run) and tool calls an agent stopped on (answer with answer_tool_approval). "
            "Each item says which tool answers it and the ids to pass."
        ),
        input_schema={"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["run_question", "tool_approval"],
                     "description": "Only this kind."},
        }},
        handler=_list_waiting, kind="read", area="approvals", paginated=True,
    )]
