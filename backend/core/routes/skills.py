"""
Skills CRUD — the library half of core/skills.py.

Disk-sourced skills (`.agents/skills/`) are listed alongside library ones but
are read-only here: they belong to the filesystem, not the store. Creating a
library skill with a disk skill's name is allowed and shadows it — that is
how a user takes ownership of an installed skill to edit it.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import skills as skills_mod

router = APIRouter()


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=skills_mod.MAX_NAME_CHARS)
    description: str = Field(min_length=1, max_length=skills_mod.MAX_DESCRIPTION_CHARS)
    body: str = Field(default="", max_length=200_000)


class SkillInstallIn(BaseModel):
    package: str = Field(min_length=1, max_length=200)


def _validated(payload: SkillIn) -> dict:
    name = skills_mod.normalize_name(payload.name)
    if not skills_mod._NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' is not a usable skill name (letters, digits, ., _, -)",
        )
    return {
        "id": name,
        "name": name,
        "description": payload.description.strip(),
        "body": payload.body,
    }


@router.get("/api/skills")
async def get_skills():
    """Library + disk, metas only (no bodies)."""
    return await skills_mod.list_skills()


@router.get("/api/skills/{name}")
async def get_skill(name: str):
    skill = await skills_mod.get_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.get("/api/skills-cli")
async def skills_cli_status():
    """Whether installing from the Skills CLI is possible here.

    The screen asks before offering the button, so a machine without Node says
    so up front instead of failing at the end of a form.
    """
    return {"available": skills_mod.npx_available()}


@router.post("/api/skills/install")
async def install_skill(payload: SkillInstallIn):
    """Install a skill with `npx skills add <package> -g -y`.

    See `core/skills.py` for why this is narrow: one validated argv element, no
    shell, user-level install, and reachable from the UI only — never as a tool
    an agent can call.
    """
    try:
        return await skills_mod.install_from_cli(payload.package)
    except skills_mod.SkillInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skills")
async def create_skill(payload: SkillIn):
    document = _validated(payload)
    library = await skills_mod.load_library()
    if any((item.get("name") or item.get("id")) == document["name"] for item in library):
        raise HTTPException(status_code=409, detail=f"Skill '{document['name']}' already exists")
    library.append(document)
    await skills_mod.save_library(library)
    return document


@router.put("/api/skills/{name}")
async def update_skill(name: str, payload: SkillIn):
    name = skills_mod.normalize_name(name)
    document = _validated(payload)
    if document["name"] != name:
        # A rename is a delete + create the UI performs explicitly; a PUT that
        # silently changes the key would orphan every agent selecting it.
        raise HTTPException(status_code=400, detail="Skill names cannot be changed in place")
    library = await skills_mod.load_library()
    for i, item in enumerate(library):
        if (item.get("name") or item.get("id")) == name:
            library[i] = document
            await skills_mod.save_library(library)
            return document
    raise HTTPException(status_code=404, detail="Skill not found in the library (disk skills are read-only)")


@router.delete("/api/skills/{name}")
async def delete_skill(name: str):
    name = skills_mod.normalize_name(name)
    library = await skills_mod.load_library()
    remaining = [item for item in library if (item.get("name") or item.get("id")) != name]
    if len(remaining) == len(library):
        raise HTTPException(status_code=404, detail="Skill not found in the library (disk skills are read-only)")
    await skills_mod.save_library(remaining)
    return {"status": "success"}
