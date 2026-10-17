"""What a tool says it does: four behaviour tags, MCP's tool annotations by our names (TAG1, D296).

    read_only    reads and changes nothing                   readOnlyHint
    destructive  changes something in a way not undone       destructiveHint
    idempotent   safe to repeat: a second call changes        idempotentHint
                 nothing the first did not
    open_world   reaches systems outside this workspace      openWorldHint

A custom HTTP tool gets them from its method when it is created or imported (F488 began this), the person sees and
changes them in the tool editor, and they are read by the tool policy (`core.tool_policy.decide`), by a Tool step
before it repeats a failed call (`may_repeat`), and by the AI builder's checks (`core.builder_guard`).

Stored as MCP's hints on the tool (`annotations`), so a custom tool, a server's tool and our own product tools all
speak one language; this module is only the names, the method table and the rules for reading and tightening. It
imports nothing from the engine, so the product tools layer (`core.product_tools`) and a host can use it.

Naveen's calls (2026-10-17): every HTTP tool reaches outside unless the person unticks it (n8n at localhost still
sends the email); a tool saved before F488 stays "not set" until someone saves it; a Python tool starts not set.
"""
from __future__ import annotations

TAGS = ("read_only", "destructive", "idempotent", "open_world")

#: Who wrote a tool's tags when it was not a person: a file's arrived with it (a bundle, a transfer between
#: workspaces), and are a claim until a person saves the tool, as the AI builder's are (`core.tools.BUILDER_HINTS`).
IMPORT_HINTS = "import"

HINT_OF = {"read_only": "readOnlyHint", "destructive": "destructiveHint",
           "idempotent": "idempotentHint", "open_world": "openWorldHint"}

_READ = {"read_only": True, "destructive": False, "idempotent": True, "open_world": True}
_WRITE = {"read_only": False, "destructive": False, "idempotent": False, "open_world": True}

#: What an HTTP method implies (RFC 9110 §9.2: GET, HEAD, OPTIONS are safe; PUT and DELETE are idempotent). Read by
#: `test_tool_tags_agree`, which holds the tool editor's copy (`frontend/src/lib/toolHints.ts`) to it.
METHOD_TAGS: dict[str, dict[str, bool]] = {
    "GET": _READ, "HEAD": _READ, "OPTIONS": _READ,
    "PUT": {**_WRITE, "idempotent": True},
    "POST": _WRITE,
    "PATCH": _WRITE,
    "DELETE": {**_WRITE, "destructive": True, "idempotent": True},
}


def defaults_for(method: str | None) -> dict[str, bool]:
    """The tags an HTTP tool starts with. A method nobody listed is taken as a write."""
    return dict(METHOD_TAGS.get((method or "GET").upper(), _WRITE))


def hints_of(tags: dict) -> dict[str, bool]:
    """MCP's hints for `tags`. A read-only tool carries no destructiveHint: "destructiveHint is meaningful only when
    readOnlyHint is false" (MCP), and F488's saved GETs carry the three."""
    out = {HINT_OF[t]: bool(tags.get(t)) for t in TAGS}
    if out["readOnlyHint"]:
        del out["destructiveHint"]
    return out


def tags_from(hint) -> dict[str, bool]:
    """The tags, given `hint(name)` that answers one MCP hint or None. The one reading rule, used by
    `core.tools.tool_hints` for live tools as well as by `tags_of` for stored ones."""
    read_only = bool(hint("readOnlyHint"))
    return {
        "read_only": read_only,
        # MCP's default for an absent destructiveHint is true; it means nothing on a read-only tool.
        "destructive": (not read_only) and hint("destructiveHint") is not False,
        # Not inferred from read_only: reading the clock changes nothing and still answers differently every call.
        "idempotent": bool(hint("idempotentHint")),
        "open_world": bool(hint("openWorldHint")),
    }


def tags_of(annotations) -> dict[str, bool] | None:
    """The tags stored hints say, or None when there are none (the tool is "not set", the policy's `undeclared`)."""
    if not isinstance(annotations, dict):
        return None
    return tags_from(annotations.get)


def clean_hints(annotations) -> dict[str, bool] | None:
    """`annotations` with only the four hints, each a boolean, as a write path stores them; None when not a dict."""
    if not isinstance(annotations, dict):
        return None
    return {h: bool(annotations[h]) for h in HINT_OF.values() if h in annotations}


def tighten(defaults: dict, claimed: dict | None) -> dict[str, bool]:
    """`defaults` with only the claims that make a tool stricter: what the AI builder may add to its method's tags.

    It can say a GET deletes something or that a call is not safe to repeat; it cannot say a POST only reads, that a
    write is safe to repeat, or that a tool stays inside. Its hints are a claim, believed when they restrict, as an
    outside server's are (F488).
    """
    tags = dict(defaults)
    for tag, value in (claimed or {}).items():
        if tag not in TAGS or not isinstance(value, bool):
            continue
        if tag in ("destructive", "open_world") and value:
            tags[tag] = True
        elif tag in ("read_only", "idempotent") and not value:
            tags[tag] = False
    if tags["destructive"]:
        tags["read_only"] = False
    return tags


def may_repeat(tool: dict) -> bool:
    """Whether a custom tool's failed call may be made again when it may already have reached the server (F737).

    A read changes nothing, so it may; otherwise the tool's own "safe to repeat". A tool with no tags: an HTTP tool's
    method decides, as RFC 9110 does; a Python tool keeps the retry it always had.
    """
    tags = tags_of(tool.get("annotations"))
    if tags is None:
        if (tool.get("tool_type") or "http") != "http":
            return True
        tags = defaults_for(tool.get("method"))
    return tags["read_only"] or tags["idempotent"]


def carried(tool: dict, before: dict | None) -> dict:
    """`tool`, about to replace `before` (the saved tool of the same name), with `before`'s tags when its method has
    not changed: importing a spec again keeps what the person set (F734). A changed method takes its new defaults."""
    if (isinstance(before, dict) and isinstance(before.get("annotations"), dict)
            and (before.get("method") or "GET").upper() == (tool.get("method") or "GET").upper()):
        tool["annotations"] = dict(before["annotations"])
        if before.get("annotations_by"):
            tool["annotations_by"] = before["annotations_by"]
        else:
            tool.pop("annotations_by", None)
    return tool


#: Said when a call that may have gone through is not made again (F737).
MAY_HAVE_RUN = "it may have run; check before running it again"


def may_have_reached(exc: BaseException) -> bool:
    """Whether a failed call may have reached the tool's server and had its effect (F737).

    A timeout waiting for the answer, an answer cut off, an MCP request that timed out: the request was out. A refused
    or timed-out connection, or a pool with no room, never sent it.
    """
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return True
    try:
        from mcp.shared.exceptions import McpError
    except Exception:  # pragma: no cover - the SDK ships with the engine
        return False
    return isinstance(exc, McpError) and getattr(getattr(exc, "error", None), "code", None) == 408


def listed_hints(tool) -> dict[str, bool] | None:
    """The four hints a listed tool (an MCP `Tool`, or a dict) declares, as a host stores them with the server's tool
    list; None when it declares none (F740). Only the hints it set: an absent one keeps MCP's meaning."""
    ann = tool.get("annotations") if isinstance(tool, dict) else getattr(tool, "annotations", None)
    if ann is None:
        return None
    if not isinstance(ann, dict):
        ann = {h: getattr(ann, h, None) for h in HINT_OF.values()}
    return {h: bool(ann[h]) for h in HINT_OF.values() if ann.get(h) is not None}
