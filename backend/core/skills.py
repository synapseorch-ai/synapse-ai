"""
Skills — reusable instruction packages an agent can load on demand.

The format is Claude Code's, deliberately: YAML frontmatter carrying `name`
and `description`, then a markdown body. That shape is already on disk in
thousands of repos, which means a skill written for one product works in the
other and nobody maintains a converter. Unknown frontmatter keys are kept but
ignored, for the same compatibility reason.

Two sources, one list:

* the **library** — a tenant-scoped store collection, created and edited in
  the UI; the one place to add a skill that works identically in every
  deployment of the engine;
* the **disk** — `.agents/skills/` scanned at the install root (and
  `SYNAPSE_SKILLS_DIR` when set), read-only, so skills already on the machine
  are offered without an import step. Deployments whose images don't carry
  the directory simply find nothing. On a name collision the library wins —
  the user made that one deliberately — and the disk copy is flagged shadowed.

Progressive disclosure is the design (decided before the editor was built):
an agent's selected skills cost only their name and one-line description in
the system prompt, every turn. The body enters the context solely through
the native `load_skill` tool — the engine-owned interception pattern
`update_tasks` established — so a skill is nearly free until the moment it
is actually used.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SKILL_TOOL_NAME = "load_skill"

#: A loaded body larger than this is truncated with a notice — a skill is
#: instructions, not a dataset; anything bigger belongs in the vault.
MAX_SKILL_BODY_CHARS = 40_000

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def normalize_name(raw: str) -> str:
    """Lower-case, spaces to hyphens — the kebab-case the format expects."""
    return re.sub(r"\s+", "-", str(raw or "").strip().lower())[:MAX_NAME_CHARS]


def parse_skill_markdown(text: str) -> tuple[dict | None, str | None]:
    """One SKILL.md → ({name, description, body}, None), or (None, why not).

    `name` and `description` are required; everything else in the frontmatter
    is tolerated and dropped, because other tools' skills carry keys this
    engine has no use for.
    """
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return None, "no YAML frontmatter (expected a leading '---' block)"
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        return None, f"frontmatter is not valid YAML: {exc}"
    if not isinstance(meta, dict):
        return None, "frontmatter must be a YAML mapping"

    name = normalize_name(str(meta.get("name") or ""))
    if not name:
        return None, "frontmatter has no 'name'"
    if not _NAME_RE.match(name):
        return None, f"'{name}' is not a usable skill name (letters, digits, ., _, -)"
    description = str(meta.get("description") or "").strip()
    if not description:
        return None, "frontmatter has no 'description'"

    return {
        "name": name,
        "description": description[:MAX_DESCRIPTION_CHARS],
        "body": text[match.end():].strip(),
    }, None


# ── library (store-backed) ───────────────────────────────────────────────────

async def load_library() -> list[dict]:
    from core.store import collections
    return await collections.load("skills")


async def save_library(items: list[dict]) -> None:
    from core.store import collections
    await collections.save("skills", items)


# ── disk (read-only) ─────────────────────────────────────────────────────────

def _disk_roots() -> list[Path]:
    """Where installed skills are looked for, most specific first.

    The home directory is in this list because that is where people actually
    keep them: a skill is curated by a person and reused across projects, the
    same way Claude Code has personal skills alongside a project's own. Scanning
    only the install root found nothing on a machine whose skills all lived in
    `~/.agents/skills`, which is the ordinary case rather than the exotic one.

    Order is precedence — the first root to define a name wins — so an install's
    own skills override the operator's personal copies of the same name.
    """
    roots: list[Path] = []
    env_dir = os.environ.get("SYNAPSE_SKILLS_DIR", "").strip()
    if env_dir:
        roots.append(Path(env_dir))
    backend_root = Path(__file__).resolve().parent.parent
    roots.append(backend_root.parent / ".agents" / "skills")
    roots.append(Path.cwd() / ".agents" / "skills")
    try:
        roots.append(Path.home() / ".agents" / "skills")
    except RuntimeError:
        pass  # no resolvable home (some service accounts); the others still apply
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            key = root.resolve()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def scan_disk_skills() -> list[dict]:
    """Skills already on the machine: `<root>/<slug>/SKILL.md` or `<root>/<name>.md`.

    Per-file failures are logged and skipped — one malformed skill must not
    hide the rest.
    """
    found: dict[str, dict] = {}
    for root in _disk_roots():
        if not root.is_dir():
            continue
        candidates: list[Path] = []
        try:
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    candidates.append(entry / "SKILL.md")
                elif entry.suffix.lower() == ".md":
                    candidates.append(entry)
        except OSError as exc:
            logger.warning("skills: cannot scan %s: %s", root, exc)
            continue
        for path in candidates:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("skills: cannot read %s: %s", path, exc)
                continue
            skill, err = parse_skill_markdown(text)
            if skill is None:
                logger.warning("skills: skipping %s: %s", path, err)
                continue
            skill["source"] = "disk"
            skill["path"] = str(path)
            # First root wins for duplicate names across roots.
            found.setdefault(skill["name"], skill)
    return list(found.values())


# ── the merged view ──────────────────────────────────────────────────────────

async def list_skills(include_bodies: bool = False) -> list[dict]:
    """Library + disk, one list. Library shadows disk on a name collision."""
    library = await load_library()
    by_name: dict[str, dict] = {}
    for item in library:
        skill = {
            "name": item.get("name") or item.get("id"),
            "description": item.get("description", ""),
            "body": item.get("body", ""),
            "source": "library",
        }
        if skill["name"]:
            by_name[skill["name"]] = skill

    result = list(by_name.values())
    for disk_skill in scan_disk_skills():
        entry = dict(disk_skill)
        entry["shadowed"] = entry["name"] in by_name
        result.append(entry)

    if not include_bodies:
        result = [{k: v for k, v in s.items() if k != "body"} for s in result]
    return result


async def get_skill(name: str) -> dict | None:
    """One skill with its body, library first."""
    name = normalize_name(name)
    for item in await load_library():
        if (item.get("name") or item.get("id")) == name:
            return {
                "name": name,
                "description": item.get("description", ""),
                "body": item.get("body", ""),
                "source": "library",
            }
    for skill in scan_disk_skills():
        if skill["name"] == name:
            return skill
    return None


async def resolve_selected(names: list[str]) -> list[dict]:
    """The metas (no bodies) for an agent's selected skill names, in order.

    Unknown names are skipped and logged, not fatal — a skill deleted after
    an agent selected it must not break the agent.
    """
    available = {s["name"]: s for s in await list_skills() if not s.get("shadowed")}
    resolved: list[dict] = []
    for raw in names or []:
        name = normalize_name(raw)
        skill = available.get(name)
        if skill is None:
            logger.warning("skills: agent selects unknown skill '%s' — skipped", name)
            continue
        resolved.append(skill)
    return resolved


def render_roster(skills: list[dict]) -> str:
    """The system-prompt block: names and descriptions, never bodies."""
    lines = [
        "### SKILLS",
        f"You have the following skills. Before applying one, call `{SKILL_TOOL_NAME}` "
        "with its name to read its full instructions — the descriptions below are only "
        "summaries. Load a skill when its subject matches the task at hand; do not load "
        "skills you will not use.",
    ]
    for skill in skills:
        lines.append(f"- `{skill['name']}`: {skill['description']}")
    return "\n".join(lines)


def build_load_skill_tool(skill_names: list[str]):
    """The runtime VirtualTool, its enum narrowed to the agent's selection."""
    from core.tools import VirtualTool

    return VirtualTool(
        name=SKILL_TOOL_NAME,
        description=(
            "Load a skill's full instructions into your context. Call this before "
            "applying a skill listed in your SKILLS section."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": skill_names,
                    "description": "The skill to load",
                },
            },
            "required": ["name"],
        },
    )


def clip_body(body: str) -> str:
    if len(body) > MAX_SKILL_BODY_CHARS:
        omitted = len(body) - MAX_SKILL_BODY_CHARS
        return body[:MAX_SKILL_BODY_CHARS] + f"\n\n[...skill truncated — {omitted} chars omitted]"
    return body
