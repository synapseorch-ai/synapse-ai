"""What the Widgets screen does, once, for every door that does it (F1262, REACH2; the one-place rule, F1189).

The screen's routes (`core.routes.widget`'s owner surface) and the product tools Synapse AI's cards press
(`core.product_tools.widgets`) call these, so each door says the same thing for the same reason. Plain async functions
over dicts: no request, no response type. A refusal is a `WidgetRefused` with the words the screen has always shown and
the HTTP status its route answers with.

A host whose widgets live elsewhere (cloud: its own table, roles, plan counts, its audit) does each act its own way;
these stay self-hosted's.
"""
from __future__ import annotations

from typing import Any

from core.chat_widgets import branding as _branding
from core.chat_widgets import store as _store
from core import visitor as _visitor

#: What a person sets on a widget, as the screen's form sends it (`routes.widget.WidgetBody`). The logo is not here:
#: it is bytes, on its own door (`set_logo`).
FIELDS = ("name", "agent_id", "allowed_origins", "enabled", "greeting", "accent", "position", "launcher_label",
          "title", "daily_message_cap", "visitor_tools", "font", "theme", "header_style", "launcher")


class WidgetRefused(Exception):
    """Why the widget was not saved, in the screen's words, with the status its route answers."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail, self.status = detail, status


def body(payload: dict) -> dict:
    """A widget as the screen's form sends it, held to the form's own rules (`WidgetBody`)."""
    from pydantic import ValidationError

    from core.routes.widget import WidgetBody

    try:
        return WidgetBody.model_validate(payload).model_dump()
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc") or ())
        raise WidgetRefused(f"{where}: {first.get('msg') or 'not valid'}" if where else str(exc), 422) from None


async def refuse_an_unpublishable_agent(agent_id: str) -> None:
    """A widget will not bind the AI builder's own agents.

    They create and change the workspace's agents and orchestrations, and are
    nobody's to publish; the agents list already hides them and v1 refuses them
    (`api_v1._is_system_agent`). Measured before this: a widget on
    `agent_native_builder_saver_create` was accepted.

    Checked at bind time, so it fails in front of the owner rather than in front
    of a visitor mid-sentence, and again before every turn.

    This used to refuse an agent whose tool policy would stop and ask a person.
    That refusal pushed owners to `allow` — the one policy under which a visitor
    drove every tool the agent held — and it is no longer needed: a visitor's
    `confirm` is a `deny` (`core.visitor`).
    """
    if not agent_id:
        return
    from core.scale.context import resolve_agent

    try:
        agent = await resolve_agent(agent_id)
    except Exception:  # noqa: BLE001 — a missing agent is the next check's problem
        agent = None
    if agent is None:
        raise WidgetRefused(f"No agent '{agent_id}'.", 404)
    if agent.get("system") or agent.get("type") == "builder":
        raise WidgetRefused(
            "The AI builder's own agents cannot be published on a widget. Choose one of your agents.", 409)


async def visitor_tool_choices(agent_id: str) -> list[dict]:
    """What an owner can let a widget's visitors use: every tool a visitor's turn
    on this agent could reach, each with what it does.

    For an orchestrator agent that is its workflow's — each Agent step's agent's
    tools and each Tool step's tool, through nested workflows — because a
    visitor's turn runs the workflow and the list holds in every step (F280).
    The engine's own tools (the task list, skills) are always a visitor's and
    memory never is, so neither is offered here.

    `asks_first` (F1524): whether the policy the tool runs under asks a person
    before it, which on a widget or a channel nobody can answer, so there the
    call is refused (`core.visitor`). It is the agent's own policy over the
    workspace's floor, as the engine resolves it: an agent's tools by that
    agent's, a workflow's Tool step by its own agent's (`steps.py`). A tool
    reached twice asks first if either asks. `category` stays what the tool
    does, as before.
    """
    import core.server as _server
    from core.config import load_settings
    from core.scale.context import resolve_agent, resolve_custom_tools, resolve_orchestration
    from core.tool_policy import DEFAULT_POLICY, decide, resolve_policy
    from core.tools import aggregate_all_tools, resolve_one_tool

    agent = await resolve_agent(agent_id) if agent_id else None
    if agent is None:
        raise WidgetRefused(f"No agent '{agent_id}'.", 404)
    custom = await resolve_custom_tools()
    settings = load_settings()
    found: dict[str, dict] = {}

    def note(tool, holder: dict | None) -> None:
        name = getattr(tool, "name", "") or ""
        if not name or name in _visitor.ENGINE_TOOLS or name in _visitor.NEVER_FOR_VISITORS:
            return
        asks = decide(tool, resolve_policy(holder or {}, settings))[0] == "confirm"
        if name in found:
            found[name]["asks_first"] = found[name]["asks_first"] or asks
            return
        found[name] = {"name": name, "category": decide(tool, DEFAULT_POLICY)[1], "asks_first": asks}

    async def walk(node: dict, depth: int) -> None:
        if node.get("type") == "orchestrator":
            # Three levels, as the engine nests.
            orch = await resolve_orchestration(node.get("orchestration_id") or "") if depth < 3 else None
            for step in (orch or {}).get("steps") or []:
                if step.get("type") == "tool" and step.get("forced_tool"):
                    tool = await resolve_one_tool(_server, step["forced_tool"], {}, custom)
                    holder = await resolve_agent(step["agent_id"]) if step.get("agent_id") else None
                    note(tool if tool is not None else _named(step["forced_tool"]), holder)
                elif step.get("type") == "agent" and step.get("agent_id"):
                    child = await resolve_agent(step["agent_id"])
                    if child is not None:
                        await walk(child, depth + 1)
            return
        tools, *_ = await aggregate_all_tools(_server, node, custom)
        for tool in tools:
            note(tool, node)
        if "spawn_subtask" in (node.get("tools") or []):
            note(_named("spawn_subtask"), node)
        if node.get("type") == "delegate":
            note(_named("delegate_to_agent"), node)
        # Knowledge's tools, over the agent's bases opened to visitors (D153 #12):
        # with none, a ticked tool would reach nothing.
        from core.knowledge import tools as knowledge_tools

        visitor_bases = await knowledge_tools.agent_bases(node, visitor=True)
        if visitor_bases:
            for tool in knowledge_tools.build_tools(visitor_bases):
                note(tool, node)

    await walk(agent, 0)
    return [found[n] for n in sorted(found)]


def _named(name: str):
    """A tool known only by name — it declares nothing, so it reads `undeclared`."""
    from core.tools import VirtualTool

    return VirtualTool(name, "", {}, None)


async def reachable_visitor_tools(agent_id: str, names: list[str], choices: list[dict] | None = None) -> list[dict]:
    """`names`, each with its category, when the agent's visitor turn can reach every one; else the ones it can't, by
    name (TRI1, F1262: a card never lets a visitor hold a tool by a name the agent doesn't have)."""
    choices = choices if choices is not None else await visitor_tool_choices(agent_id)
    known = {c["name"]: c for c in choices}
    wanted = _visitor.clean_tools(names)
    missing = [n for n in wanted if n not in known]
    if missing:
        offered = ", ".join(sorted(known)) or "none"
        raise WidgetRefused(f"The agent's visitor turn can't reach {', '.join(missing)}. It can offer: {offered}.", 422)
    # What it does, and whether it asks first and so is refused for a visitor (F1524).
    return [{"name": n, "category": known[n]["category"], "asks_first": bool(known[n].get("asks_first"))}
            for n in wanted]


# ── the owner's acts ─────────────────────────────────────────────────────────

async def list_widgets() -> list[dict]:
    return await _store.list_widgets()


async def get(widget_id: str) -> dict:
    widget = await _store.get_widget(widget_id) if widget_id else None
    if widget is None:
        raise WidgetRefused("No such widget.", 404)
    return widget


async def create(payload: dict) -> dict:
    """A new widget, as the screen's form makes it."""
    fields = body(payload)
    await refuse_an_unpublishable_agent(fields["agent_id"])
    return await _store.save_widget(fields)


async def replace(widget_id: str, payload: dict) -> dict:
    """The screen's save: the whole form, over the widget that is there."""
    await get(widget_id)
    fields = body(payload)
    await refuse_an_unpublishable_agent(fields["agent_id"])
    return await _store.save_widget({**fields, "id": widget_id})


async def update(widget_id: str, changes: dict) -> dict:
    """Only the fields named, the rest as they are: what a card changes. The logo and the key are kept."""
    widget = await get(widget_id)
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise WidgetRefused(f"A widget has no {', '.join(unknown)}.", 422)
    fields = body({**{k: widget.get(k) for k in FIELDS if widget.get(k) is not None}, **changes})
    if "agent_id" in changes:
        await refuse_an_unpublishable_agent(fields["agent_id"])
    return await _store.save_widget({**widget, **fields, "id": widget_id})


async def delete(widget_id: str) -> None:
    if not await _store.delete_widget(widget_id):
        raise WidgetRefused("No such widget.", 404)


async def rotate_key(widget_id: str) -> dict:
    """Issue a new public key. Every page carrying the old snippet stops working,
    which is the point — this is what an abused key is replaced with."""
    widget = await _store.rotate_public_key(widget_id)
    if widget is None:
        raise WidgetRefused("No such widget.", 404)
    return widget


async def set_logo(widget_id: str, raw: bytes) -> str:
    """Replace this widget's logo.

    Stored as a `data:` URI on the widget and inlined by the embed page, rather
    than served from a route of its own. A visitor is anonymous, so a logo route
    would be a *second* public surface with its own cache and abuse story — for
    an image drawn at 28 pixels.

    The type comes from the magic bytes, never the filename or the browser's
    `Content-Type`: both are attacker-controlled, and this string ends up in a
    `data:` URI the browser will honour.
    """
    import base64

    from core.images import sniff

    widget = await get(widget_id)
    if not raw:
        raise WidgetRefused("That file is empty.", 422)
    if len(raw) > _branding.MAX_LOGO_BYTES:
        raise WidgetRefused(
            f"A logo has to be under {_branding.MAX_LOGO_BYTES // 1024} KB. "
            "It is drawn at 28 pixels, so a small one is not a compromise.", 413)
    mime = sniff(raw)
    if mime is None:
        raise WidgetRefused(
            "That is not a PNG, JPEG, GIF or WebP. Renaming a file does not change what is inside it.", 422)
    widget["logo"] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    await _store.save_widget(widget)
    return widget["logo"]


async def remove_logo(widget_id: str) -> None:
    widget = await get(widget_id)
    widget["logo"] = ""
    await _store.save_widget(widget)


async def vault_logo(path: str) -> bytes:
    """An image from the Vault, for a logo a card sets (the screen takes an upload; a card names a file): at most one
    byte over the cap is read, so `set_logo` says it is too big without the whole file in memory."""
    from fastapi import HTTPException

    from core.routes import vault

    try:
        rel = vault._safe_rel(path)
    except HTTPException:
        rel = ""
    if not rel:
        raise WidgetRefused("Say which image: its path in the Vault.", 422)
    import asyncio

    cap = _branding.MAX_LOGO_BYTES + 1
    store = vault._get_storage()

    def read() -> bytes | None:
        if store is not None:
            chunks = store.iter_bytes(vault._s3_key_for(rel))
            if chunks is None:
                return None
            out = b""
            for chunk in chunks:
                out += chunk
                if len(out) >= cap:
                    break
            return out[:cap]
        try:
            p = vault._resolve(rel)
        except HTTPException:
            return None
        if not p.is_file():
            return None
        with p.open("rb") as f:
            return f.read(cap)

    raw = await asyncio.to_thread(read)
    if raw is None:
        raise WidgetRefused(f"There is no file {rel} in the Vault.", 404)
    return raw


def snippet(base: str, public_key: str) -> str:
    """The one tag a site carries (the screen's own, `WidgetsTab.tsx`)."""
    return f'<script src="{base}/embed/v1.js" data-key="{public_key}" async></script>'


def look(widget: dict[str, Any]) -> dict[str, Any]:
    """A widget's settings as a card shows them, defaults filled in as the store fills them."""
    return {**_store.DEFAULTS, **{k: v for k, v in widget.items() if v not in (None, "")}}
