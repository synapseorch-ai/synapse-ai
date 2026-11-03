"""Skills, as product tools (F1266, REACH2): what the Skills screen deletes, for Synapse AI's reviewed change.

Writing a skill and giving it to agents are the builder's tools already (`create_skill`, `update_skill`,
`attach_skills`). Deleting one is the Skills screen's own code (`core.skills.delete_from_library`): a library skill
only, a disk skill being the folder's. Installing one from the Skills registry is never here: it is done on the Skills
screen, by a person (its route's rule, "UI only"; manager 79, 11-03).

In the app only (`app_only`) and a builder's, as on the screen.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role


async def _delete(caller: Any, args: dict) -> dict:
    from core import skills

    try:
        gone = await skills.delete_from_library(str(args.get("name") or ""))
    except skills.NotInLibrary as exc:
        raise ToolRefusal(str(exc)) from None
    return {"deleted": gone["name"]}


def entries() -> list[ProductTool]:
    return [ProductTool(
        name="delete_skill", description="Delete a skill from the workspace's library; agents that select it load "
                                         "nothing in its place until it is back.",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        handler=_delete, kind="act", area="skills", min_role=Role.BUILDER, scope="build", irreversible=True,
        app_only=True, audit_action="skill.deleted", target=lambda a, r: ("skill", str(a.get("name") or "")),
    )]
