"""Turning a custom HTTP tool plus its arguments into one request.

**One implementation, because there were two and they drifted twice.**

`react_engine.run_agent_step` and `orchestration/steps.py::ToolStepExecutor`
both executed HTTP tools, from copies of the same code. D69 fixed three things
in the agent's copy — `{{global.key}}` rendering, the request-body template, and
arguments consumed by the URL no longer being sent in the body as well — and the
orchestration copy got none of them. So the same tool, called by an agent and
called by a TOOL step, behaved differently: one resolved an organisation's
variables and one sent a literal `{{global.api_base}}` to the server.

That is exactly the shape `core/execution` was created to end for `docker run`,
and this is the same medicine. Both call sites now build their request here.

What stays at the call sites, because it genuinely differs: the agent vaults a
large response and filters it through `outputSchema`; the step returns it whole.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from core import templating

#: `{data}`, `{data.key}` and `{key}` — the single-brace forms that predate
#: `{{ … }}`. Applied after the double-brace pass, so a URL may hold either.
_SINGLE = re.compile(r"\{([^{}]+)\}")


class NotConnected(ValueError):
    """A made tool whose account part (`__dc__` in its host) is not filled in yet: it is never called (F944, A8)."""


#: A host part a made tool keeps until its Connect card fills it: never filled from arguments, not a DNS label.
_UNCONNECTED = re.compile(r"__[a-z0-9_]+__")


#: Set by the build's save while it makes a read API's tools (F944): the one channel a new lock enters by. A caller's
#: `base_lock` (an MCP client, a builder agent, a person's editor, an import) is never taken (SAI2's review, blocker 1).
import contextvars as _contextvars

MAKING_TOOLS: _contextvars.ContextVar[bool] = _contextvars.ContextVar("making_tools", default=False)


def lock_of(url: str) -> str:
    """The base a tool's URL keeps to: its scheme, host and path up to its first `{placeholder}` segment."""
    from urllib.parse import urlsplit

    got = urlsplit(url.partition("?")[0])
    path = "/".join(seg for seg in got.path.split("/")[:next(
        (i for i, seg in enumerate(got.path.split("/")) if "{" in seg), None)]).rstrip("/")
    return f"{got.scheme}://{got.netloc}{path}"


def under_lock(url: str, lock: str) -> bool:
    """Whether a tool's URL (a template) stays on its lock: the same scheme, host and port, a path at or under."""
    from urllib.parse import urlsplit

    got, locked = urlsplit(url.partition("?")[0]), urlsplit(lock)
    under = locked.path.rstrip("/")
    return ((got.scheme, got.netloc.lower()) == (locked.scheme, locked.netloc.lower())
            and (got.path.rstrip("/") == under or got.path.startswith(under + "/")))


def _render_locked(url: str, args: dict[str, Any], global_variables: dict[str, Any],
                   lock: str) -> tuple[str, set[str]]:
    """A made tool's URL (F944's A8; opt-in by `base_lock`), on top of F1014's rule for every tool (each value
    percent-encoded, never raw here, and the host the template's): a tool not connected yet is never called, and the
    URL it renders keeps the lock's scheme, host and port and a path at or under the lock's, with no `.` or `..`
    segment (which encoding leaves as written) and no fragment, or it is not sent."""
    from urllib.parse import urlsplit

    locked = urlsplit(lock)
    if _UNCONNECTED.search(locked.hostname or ""):
        raise NotConnected("This tool is not connected yet: its account part is filled in on its Connect card; "
                           "connect it first.")
    # A made tool's template has no fragment; one is never sent.
    rendered, consumed = render_url(url.partition("#")[0], args, global_variables)
    got = urlsplit(rendered)
    under = locked.path.rstrip("/")
    segments = got.path.split("/")
    if ((got.scheme, got.hostname, got.port) != (locked.scheme, locked.hostname, locked.port)
            or not (got.path.rstrip("/") == under or got.path.startswith(under + "/"))
            or "." in segments or ".." in segments or got.fragment):
        raise ValueError("This tool's call went outside the API it was made for, so it was not sent.")
    return rendered, consumed


class MissingArgument(ValueError):
    """The call left out an argument the tool's URL, a header or its body names (F196).

    Rendering leaves an unknown `{{ … }}` as written, which is right for a
    prompt and wrong for a request: the template text went to someone else's
    server (`/echo?tag={{%20tag%20}}`). A `ValueError`, so every caller that
    already reports one reports this: an agent's model reads the sentence and
    can call again, and a Tool step's next attempt is told it.
    """

    def __init__(self, names: list[str], places: list[str]):
        self.names = names
        listed = ", ".join(f"`{n}`" for n in names)
        noun = "argument" if len(names) == 1 else "arguments"
        super().__init__(
            f"The call left out the {noun} {listed}, which this tool's "
            f"{' and '.join(places)} {'needs' if len(places) == 1 else 'need'}. "
            f"Nothing was sent; call it again with {'it' if len(names) == 1 else 'them'}."
        )


class UnsafeUrl(ValueError):
    """A tool's arguments would move its request off the URL its author wrote (F1014).

    A `ValueError`, like `MissingArgument`: both callers already report one to
    the model or fail the step with it, and nothing is sent.
    """


#: One DNS label: what a placeholder in a URL's host may be filled with
#: (`https://{tenant}.zendesk.com`). No dot, slash, `@`, colon or `#`.
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _encode(text: str, raw: bool) -> str:
    """An argument's text as it goes into a URL (F1014).

    Percent-encoded to the unreserved set, so a value cannot add a path
    segment, a query parameter, a fragment, userinfo or a port. A tool that
    opts out (`url_values: "raw"`, for an API whose path argument holds
    slashes) keeps its value as written, but never its braces: a value is not
    a template and is not filled again.
    """
    if raw:
        return text.replace("{", "%7B").replace("}", "%7D")
    return quote(text, safe="-_.~")


def _argument_of(reference: str) -> str | None:
    """The argument a `{{ … }}` reference reads, or None for `global.*`/`state.*`/`{{args}}`."""
    parts = reference.split(".")
    if parts[0] == "args":
        return parts[1] if len(parts) > 1 else None
    return None if parts[0] in templating.NAMESPACES else parts[0]


def _missing(text: str, args: dict[str, Any]) -> list[str]:
    """The arguments `text`'s references read that the call did not supply."""
    out: list[str] = []
    for reference in templating.references(text):
        name = _argument_of(reference)
        if name is None or name in out:
            continue
        found, _ = templating.lookup(reference, {"args": args}, bare="args")
        if not found:
            out.append(name)
    return out


def _drop_optional_query(url: str, args: dict[str, Any], required: set[str]) -> str:
    """The URL without query parameters that are only a missing, optional argument.

    `?tag={{ tag }}` with no `tag` in the call is an optional parameter not
    given: leaving it out is what the tool's author meant. A parameter the
    schema requires, or one whose value is more than the reference, stays for
    `build_request` to refuse by name.
    """
    base, sep, rest = url.partition("?")
    if not sep:
        return url
    query, hash_sep, fragment = rest.partition("#")
    kept = []
    for pair in query.split("&"):
        _, eq, value = pair.partition("=")
        whole = templating.REF.fullmatch(value.strip()) if eq else None
        name = _argument_of(whole.group(1)) if whole else None
        if name and name not in required and _missing(value, args):
            continue
        kept.append(pair)
    query = "&".join(kept)
    return base + ("?" + query if query else "") + (hash_sep + fragment if hash_sep else "")


def _walk(obj: Any, path: str) -> str | None:
    """Follow a dot-separated path into `obj`, rendered as text."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return json.dumps(cur) if isinstance(cur, (dict, list)) else str(cur)


def _fill(url: str, args: dict[str, Any], global_variables: dict[str, Any], put, consumed: set[str]) -> str:
    """`url` with its references filled: an argument's text through `put`, the author's globals as written.

    Two passes, and the order matters. `{{ … }}` first, so `{{global.base}}` can
    expand into something that itself contains `{id}`; then the single-brace
    forms, so a URL written before this syntax existed — or pasted from an
    OpenAPI spec, whose path templating *is* single-brace — keeps working.
    """
    context = {"global": global_variables, "args": args}

    def _double(match: re.Match[str]) -> str:
        reference = match.group(1)
        found, value = templating.lookup(reference, context, bare="args")
        if not found:
            return match.group(0)
        if _argument_of(reference) is not None or reference == "args":
            head = reference.split(".")[0]
            if head not in templating.NAMESPACES and head in args:
                consumed.add(head)  # as before: a bare name is consumed, `{{args.x}}` is not
            return put(templating._text(value))
        return templating._text(value)  # global.* / state.*: the author's, not an argument

    url = templating.REF.sub(_double, url) if url and "{{" in url else url

    def _single(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr == "data":
            # The whole argument payload. Legacy, and deliberately kept: there
            # is no `{{ … }}` spelling of it, so removing it would delete a
            # capability rather than rename one.
            return put(json.dumps(args))
        if expr.startswith("data."):
            path = expr[len("data.") :]
            value = _walk(args, path)
            if value is not None:
                consumed.add(path.split(".")[0])
                return put(value)
            return match.group(0)
        if expr in args:
            consumed.add(expr)
            value = args[expr]
            return put(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
        return match.group(0)  # unrecognised — left visible, never blanked

    return _SINGLE.sub(_single, url)


def _check_origin(shape: str, rendered: str, values: dict[str, str]) -> None:
    """Refuse a rendered URL whose scheme, host or port an argument chose (F1014).

    `shape` is the URL filled with a marker for each argument's value. Where a
    marker lands in the scheme or port, an argument chose it; in the host, its
    value must be one DNS label and the rendered host must be exactly the
    template's with those labels in place. A `.`/`..` path segment the
    template did not have is refused too: it walks the request up the API.
    """
    def has_marker(text: str | None) -> list[str]:
        return [m for m in values if text and m in text]

    s, r = urlsplit(shape), urlsplit(rendered)
    if has_marker(s.scheme):
        raise UnsafeUrl("A tool's URL scheme cannot come from an argument. Nothing was sent.")
    if r.scheme.lower() != s.scheme.lower():
        raise UnsafeUrl("An argument changed this tool's URL scheme. Nothing was sent.")
    shape_host = (s.hostname or "")
    host_markers = has_marker(shape_host) + has_marker(s.netloc.rpartition("@")[0])
    expected = shape_host
    for marker in host_markers:
        value = values[marker]
        if not _LABEL.fullmatch(value):
            raise UnsafeUrl(
                f"An argument cannot choose this tool's host: {value!r} is not a single host label "
                "(letters, digits and hyphens). Nothing was sent.")
        expected = expected.replace(marker, value.lower())
    if (r.hostname or "") != expected or r.username != s.username or r.password != s.password:
        raise UnsafeUrl("An argument changed this tool's host. Nothing was sent.")
    try:
        shape_port, rendered_port = s.port, r.port
    except ValueError:
        raise UnsafeUrl("A tool's port cannot come from an argument. Nothing was sent.") from None
    if shape_port != rendered_port:
        raise UnsafeUrl("An argument changed this tool's port. Nothing was sent.")
    dots = {".", ".."}
    if [seg for seg in r.path.split("/") if seg in dots] != [seg for seg in s.path.split("/") if seg in dots]:
        raise UnsafeUrl("An argument cannot add a '.' or '..' path segment to this tool's URL. Nothing was sent.")


def render_url(
    url: str, args: dict[str, Any], global_variables: dict[str, Any], *, url_values: str | None = None,
) -> tuple[str, set[str]]:
    """The URL with its references filled in, and the argument keys it used.

    An argument's value is percent-encoded into its place, and the result must
    keep the template's scheme, host and port (F1014): a model steered by what
    it read could otherwise fill a keyed tool's host, or walk its path, and the
    request went there with the tool's headers. `url_values="raw"` keeps path
    values as written for an API that takes a path with slashes; the host check
    stays. The author's `{{global.*}}` is never encoded.

    The consumed keys are the point of the return value: an argument the URL
    already used must not be sent again as a query parameter or in the body,
    which is what the agent's copy did until D69 and this one did until now.
    """
    raw = str(url_values or "").strip().lower() == "raw"
    consumed: set[str] = set()
    rendered = _fill(url, args, global_variables, lambda text: _encode(text, raw), consumed)

    values: dict[str, str] = {}

    def _mark(text: str) -> str:
        marker = f"zzarg{len(values)}zz"
        values[marker] = text
        return marker

    shape = _fill(url, args, global_variables, _mark, set())
    if values:
        _check_origin(shape, rendered, values)
    return rendered, consumed


def build_request(
    tool: dict[str, Any], args: dict[str, Any], global_variables: dict[str, Any]
) -> dict[str, Any]:
    """Everything `httpx.request` needs: `method`, `url`, `headers`, and one of
    `params` / `json` / `content`.

    Raises `ValueError` when the tool has no URL, which is the one thing about a
    tool document this cannot work around, and `MissingArgument` when the call
    left out an argument a template names (F196) — read from the templates,
    not the rendered request, so an argument whose value holds braces is sent
    as given.
    """
    method = str(tool.get("method") or "POST").upper()
    url = tool.get("url") or ""
    if not url:
        raise ValueError("No URL configured for this tool.")

    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    url = _drop_optional_query(url, args, required)
    raw_headers = dict(tool.get("headers") or {})
    # `body_template` is where the AI builder stored the template before F2; a
    # tool saved then still sends it.
    template = tool.get("body") or tool.get("body_template") or ""

    missing: dict[str, list[str]] = {}
    for place, text in (
        ("URL", url),
        ("headers", " ".join(v for v in raw_headers.values() if isinstance(v, str))),
        # A lone `{{body}}` that is missing sends no body, as it always has:
        # nothing of the template reaches the server.
        ("body", "" if templating.REF.fullmatch(template.strip()) else template),
    ):
        for name in _missing(text, args):
            missing.setdefault(name, []).append(place)
    if missing:
        places = [p for p in ("URL", "headers", "body") if any(p in v for v in missing.values())]
        raise MissingArgument(sorted(missing), places)

    context = {"global": global_variables, "args": args}
    if tool.get("base_lock"):
        # A tool the build made from a read API keeps to its base too (F944, A8): beyond F1014's rule for every tool.
        url, consumed = _render_locked(url, args, global_variables, str(tool["base_lock"]))
    else:
        url, consumed = render_url(url, args, global_variables, url_values=tool.get("url_values"))
    headers = templating.render_mapping(raw_headers, context, bare="args")
    body, body_consumed = templating.request_body(
        template, args, {"global": global_variables}
    )
    consumed |= body_consumed

    request: dict[str, Any] = {"method": method, "url": url, "headers": headers}

    if method in ("GET", "DELETE"):
        remaining = {k: v for k, v in args.items() if k not in consumed}
        params = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
            for k, v in remaining.items()
        }
        base, sep, rest = url.partition("?")
        if not sep:
            request["params"] = params
            return request
        # A query written into the URL. httpx replaces a URL's query with
        # `params` whenever `params` is given, even an empty one, so every GET
        # or DELETE tool sent only its leftover arguments: `?static=1&tag=red`
        # went out as `?note=hi` (F233). The leftovers join the written query
        # instead, which is kept byte for byte; one it already names is not
        # sent again.
        query, hash_sep, fragment = rest.partition("#")
        named = {k for k, _ in parse_qsl(query, keep_blank_values=True)}
        extra = {k: v for k, v in params.items() if k not in named}
        if extra:
            encoded = str(httpx.QueryParams(extra))
            query = f"{query}&{encoded}" if query else encoded
        request["url"] = base + (f"?{query}" if query else "") + (hash_sep + fragment if hash_sep else "")
        return request

    if not template.strip():
        # No template: the arguments, minus the ones the URL already took.
        body = {k: v for k, v in args.items() if k not in consumed}
    if isinstance(body, str):
        request["content"] = body
    else:
        request["json"] = body
    return request


async def send(
    client: Any,
    tool: dict[str, Any],
    args: dict[str, Any],
    global_variables: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Build the request, check where it is going, and send it.

    **The check belongs here and not at the call sites.** `core/egress_guard.py`
    has refused loopback, link-local, private space and CGNAT since the scraper
    needed it, and until now the scraper was the only thing that called it — so
    a custom HTTP tool pointed at `http://169.254.169.254/latest/meta-data/`
    dialled it, from a worker that on a shared fleet holds somebody's
    credentials (S4). The builder refuses to *create* such a tool, but the Tools
    screen takes a raw document and DNS can be re-pointed after a tool is saved,
    so the write is the wrong place to check; the call is the only one that
    holds.

    Putting it in the sink rather than in the two callers is the lesson S1 and
    S2 both taught: a control on the callers is a control that the third caller
    will not have. `build_request` cannot host it — the URL is templated, so the
    address is only known after rendering, and `build_request` is synchronous.

    `EGRESS_ALLOW_HOSTS` is the escape hatch for a self-hosted install that
    legitimately calls something on its own network.

    Redirects need no separate handling: neither caller passes
    `follow_redirects`, and httpx defaults it to False, so there is no second
    hop to check. Turning it on would reopen this and would need the guard
    moved onto an event hook, the way the scraper had to.
    """
    from core import egress_guard

    request = build_request(tool, args, global_variables)
    try:
        await egress_guard.check_url(request["url"])
    except egress_guard.BlockedAddress as exc:
        # Say how to allow it where somebody can. The shipped n8n URL is
        # localhost, so without this every n8n call was refused with nothing
        # saying the refusal can be lifted (F78). Not in a multi-tenant
        # deployment: its customers cannot set the operator's environment, and
        # loopback on a shared fleet must stay shut.
        from core import tenancy

        if tenancy.is_multi_tenant():
            raise
        raise egress_guard.BlockedAddress(
            f"{exc}. If this install is meant to call it, add {exc.host} to the "
            f"EGRESS_ALLOW_HOSTS environment variable and restart.",
            exc.host,
        ) from None
    except (egress_guard.UnresolvedHost, egress_guard.MalformedUrl):
        # Neither of these names a destination, so neither is ours to refuse.
        # A host this machine cannot resolve is somebody else's network — the
        # same call the builder already makes about the same URLs at creation —
        # and httpx is about to fail the identical lookup if it really is
        # absent. A string with no scheme is usually an unrendered
        # `{{global.api_base}}`, and the error httpx raises names the variable,
        # which this one would not. Both are unsendable or unreachable anyway.
        pass
    return await client.request(**request, timeout=timeout)


def decode(response: Any, tool: dict[str, Any]) -> tuple[Any, str]:
    """`(parsed JSON or None, the text to hand back)`.

    `outputSchema` narrows a JSON object to the properties it names — but only
    when that leaves something, so a schema that matches nothing returns the
    response rather than an empty object.
    """
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON response is normal
        text = response.text or json.dumps(
            {"error": f"Empty response (Status: {response.status_code})"}
        )
        return None, text

    schema = tool.get("outputSchema") or {}
    if schema.get("properties") and isinstance(parsed, dict):
        narrowed = {k: parsed[k] for k in schema["properties"] if k in parsed}
        if narrowed:
            parsed = narrowed
    return parsed, json.dumps(parsed)
