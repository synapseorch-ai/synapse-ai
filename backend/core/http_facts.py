"""An HTTP tool's facts from a pasted curl, read by code (F1312a; Naveen 11-04).

A person who has an API's call to hand usually has it as a curl, copied from the API's docs. The builder's planner
was asked to retype it into a tool's fields, where a URL, a body or a header could come out changed. Now code reads
it:

* `curls_in(text)`: the curl commands a message or a reply holds, at most three, each to the end of its line (its
  `\\` and `^` continuations joined), a code fence or a backtick, or the next curl;
* `from_curl(command)`: what the tool is made from — its method as a shell would send it, its URL and query, the
  headers that carry no key, its body — and `key {header, scheme}` where its key goes. The door has already masked
  the key itself (`secret_scrub`'s curl rule): a header that carries one is known by its name, never its value, and
  never goes into the tool; its Connect card asks for the key once the tool is made. A key in its query becomes a
  workspace variable the person sets. What it can't take (a local file, a second URL, another scheme, an open
  quote) is said in words, as `{"problem": …}`;
* `same_shape(template, literal)`: the planner marks what varies with {{ }}; the template must keep the curl's
  scheme, host, port, path and query names, so the call never moves.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: A header that carries a key, by its name (Naveen's list, 11-04): never a tool's, its Connect card's. The door's rule.
from core.secret_scrub import KEY_HEADER

#: The most curls one text is read for.
MAX_CURLS = 3
#: A curl's text with no URL in it: the word in a sentence, never a command.
NO_URL = "it names no URL"
#: A query parameter that carries a key, by its name: a workspace variable, never a value in the tool.
KEY_QUERY = re.compile(r"(?i)^(?:api[_-]?key|apikey|key|token|access[_-]?token|auth|secret|client[_-]?secret|"
                       r"signature|sig)$")
#: A key's scheme the Connect card keeps (`change_ops.KEY_SCHEMES`); Basic is sent as the whole value, no scheme.
_SCHEMES = {"bearer": "Bearer", "token": "Token"}

_START = re.compile(r"(?i)(?<![\w/.-])curl\s")
#: Where a curl's text ends: a code fence, a backtick, or a line that does not go on.
_END = re.compile(r"```|`|\r?\n")
_CONTINUED = re.compile(r"[\\^][ \t]*\r?\n")
#: curl's options that take a value, and those that are switches; any other is a switch.
_DATA = {"-d", "--data", "--data-raw", "--data-ascii", "--data-binary"}
_FILES = {"-F", "--form", "--form-string", "-T", "--upload-file", "-K", "--config"}
_VALUED_IGNORED = {"-o", "--output", "-A", "--user-agent", "-e", "--referer", "-m", "--max-time",
                   "--connect-timeout", "--retry", "-w", "--write-out", "-x", "--proxy", "-c", "--cookie-jar",
                   "-r", "--range", "--resolve", "--cacert", "--cert", "--key", "-E"}
#: Trailing punctuation a sentence leaves on a URL.
_TRAILING = ".,;:!?)"


def curls_in(text: str) -> list[str]:
    """The curl commands in `text`, in the order written, at most `MAX_CURLS`."""
    text = _CONTINUED.sub(" ", text or "")
    starts = [m.start() for m in _START.finditer(text)]
    out: list[str] = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(text)
        chunk = text[start:stop]
        if end := _END.search(chunk):
            chunk = chunk[:end.start()]
        # The word in a sentence ("this curl works from my terminal: …") names no URL: no curl, never numbered (CAP3).
        if chunk.strip() and from_curl(chunk.strip()).get("problem") != NO_URL:
            out.append(chunk.strip())
        if len(out) == MAX_CURLS:
            break
    return out


def _looks_like_a_url(token: str) -> bool:
    return "://" in token or bool(re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/|$)", token))


def _key_of(name: str, value: str) -> dict[str, str]:
    scheme = value.strip().split(" ", 1)[0].lower() if " " in value.strip() else ""
    return {"header": name, "scheme": _SCHEMES.get(scheme, "")}


def _variable(host: str, param: str) -> str:
    label = (host.split(".")[0] if host else "API") or "API"
    return re.sub(r"[^A-Z0-9]+", "_", f"{label}_{param}".upper()).strip("_")


def from_curl(command: str) -> dict[str, Any]:
    """One curl as a tool's facts: `{method, url, headers, body, key, variables}`, or `{"problem": why}`."""
    text = _CONTINUED.sub(" ", command or "").strip()
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return {"problem": "its quotes don't close: paste the curl as it is in the docs"}
    if not tokens or tokens[0].lower() != "curl":
        return {"problem": "it is not a curl command"}
    method, urls, headers, data, json_body, get = "", [], [], [], None, False
    key: dict[str, str] | None = None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        flag, _, inline = token.partition("=") if token.startswith("--") else (token, "", "")

        def value() -> str:
            nonlocal i
            if inline:
                return inline
            i += 1
            return tokens[i] if i < len(tokens) else ""

        if flag in ("-X", "--request"):
            method = value().upper()
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
        elif flag in ("-H", "--header"):
            headers.append(value())
        elif flag in _DATA or flag == "--data-urlencode":
            got = value()
            if got.startswith("@"):
                return {"problem": "it sends a local file (@…), which a tool can't read: paste the body itself"}
            data.append(got)
        elif flag == "--json":
            got = value()
            if got.startswith("@"):
                return {"problem": "it sends a local file (@…), which a tool can't read: paste the body itself"}
            json_body = got
        elif flag in _FILES:
            return {"problem": "it uploads a file or reads a config file, which a tool can't: paste a call that "
                               "sends its body inline"}
        elif flag in ("-G", "--get"):
            get = True
        elif flag in ("-u", "--user"):
            value()
            key = key or {"header": "Authorization", "scheme": ""}
        elif flag == "--oauth2-bearer":
            value()
            key = key or {"header": "Authorization", "scheme": "Bearer"}
        elif flag in ("-b", "--cookie"):
            value()
            key = key or {"header": "Cookie", "scheme": ""}
        elif flag == "--url":
            urls.append(value())
        elif flag in _VALUED_IGNORED:
            value()
        elif token.startswith("-"):
            pass  # a switch (-s, -L, -k, --compressed …): nothing the tool keeps
        elif _looks_like_a_url(token.rstrip(_TRAILING)):
            urls.append(token.rstrip(_TRAILING))
        # any other word is the sentence around the curl
        i += 1

    if not urls:
        return {"problem": NO_URL}
    if len(urls) > 1:
        return {"problem": "it calls more than one URL: a tool calls one URL, so paste one call at a time"}
    url = urls[0] if "://" in urls[0] else f"https://{urls[0]}"
    got = urlsplit(url)
    if got.scheme not in ("http", "https") or not got.hostname:
        return {"problem": f"a tool calls http or https, not {got.scheme or 'that'}"}

    plain: dict[str, str] = {}
    for line in headers:
        name, _, said = line.partition(":")
        name, said = name.strip(), said.strip()
        if not name:
            continue
        if KEY_HEADER.match(name) and "{{" not in said:  # a reference names a variable: it stays, as F1309's does
            key = key or _key_of(name, said)
        elif name.lower() != "host":
            plain[name] = said
    body: str | None = json_body
    if json_body is not None:
        plain.setdefault("Content-Type", "application/json")
        plain.setdefault("Accept", "application/json")
    query = parse_qsl(got.query, keep_blank_values=True)
    if data and get:
        for chunk in data:
            query += parse_qsl(chunk, keep_blank_values=True)
    elif data:
        body = "&".join(data)
    variables: list[str] = []
    kept: list[tuple[str, str]] = []
    for name, said in query:
        if KEY_QUERY.match(name):
            variables.append(_variable(got.hostname or "", name))
            kept.append((name, f"{{{{ global.{variables[-1]} }}}}"))
        else:
            kept.append((name, said))
    rebuilt = urlunsplit((got.scheme, got.netloc, got.path, "", ""))
    if kept:
        rebuilt += "?" + "&".join(f"{n}={v}" if v.startswith("{{") else urlencode([(n, v)]) for n, v in kept)
    method = method or ("POST" if body is not None else "GET")
    out: dict[str, Any] = {"method": method, "url": rebuilt, "headers": plain, "body": body, "key": key}
    if variables:
        out["variables"] = variables
    return out


_PLACEHOLDER = re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_.]*\s*\}\}$")


def _same_part(template: str, literal: str) -> bool:
    return template == literal or bool(_PLACEHOLDER.match(template))


def same_shape(template: str, literal: str) -> str | None:
    """Why `template` (the planner's URL, {{ }} where a value varies) is not `literal` (the curl's), or None: the
    same scheme, host and port, the same path but its varied segments, the same query names."""
    a, b = urlsplit((template or "").strip()), urlsplit((literal or "").strip())
    if (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()):
        return f"it calls {a.scheme}://{a.netloc}, and the curl calls {b.scheme}://{b.netloc}"
    pa, pb = a.path.rstrip("/").split("/"), b.path.rstrip("/").split("/")
    if len(pa) != len(pb) or not all(_same_part(x, y) for x, y in zip(pa, pb)):
        return f"its path is {a.path or '/'}, and the curl's is {b.path or '/'}"
    qa, qb = parse_qsl(a.query, keep_blank_values=True), parse_qsl(b.query, keep_blank_values=True)
    if [n for n, _ in qa] != [n for n, _ in qb] or not all(_same_part(x, y) for (_, x), (_, y) in zip(qa, qb)):
        return f"its query is ?{a.query}, and the curl's is ?{b.query}"
    return None


def _keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_keys(v) for v in value[:1]]
    return None


def same_body(template: str, literal: str) -> str | None:
    """Why `template` (the planner's body, {{ }} where a value varies) is not `literal` (the curl's), or None: a JSON
    body keeps the curl's keys; any other body is the planner's to mark."""
    try:
        want = json.loads(literal)
    except (TypeError, ValueError):
        return None
    if not isinstance(want, (dict, list)):
        return None
    try:
        got = json.loads(re.sub(r"\{\{[^}]*\}\}", "0", template or ""))
    except ValueError:
        return "its body is the curl's JSON with {{ }} only where a value varies"
    return None if _keys(got) == _keys(want) else "its body keeps the curl's keys, with {{ }} only where a value varies"


def reads(text: str) -> list[str]:
    """The arguments a URL, a header or a body reads (`{{ order_id }}`), as a call fills them; never a workspace
    variable or the run's state."""
    from core import http_tool, templating

    found: list[str] = []
    for reference in templating.references(text or ""):
        if (name := http_tool._argument_of(reference)) and name not in found:
            found.append(name)
    return found


def new_inputs(schema: dict | None, url: str) -> tuple[dict, list[str]]:
    """A tool's input schema with each argument its new address reads that it lacks (F1312b): one in the path is
    required, one only in the query may be left out, as a call drops an optional query value it isn't given. None is
    ever taken away. The schema, and the names it gained."""
    out = {"type": "object", **(schema if isinstance(schema, dict) else {})}
    properties = dict(out.get("properties") or {})
    required = list(out.get("required") or [])
    got = urlsplit((url or "").strip())
    in_path = set(reads(got.path)) | set(reads(got.netloc))
    added: list[str] = []
    for name in reads(url):
        if name in properties:
            continue
        properties[name] = {"type": "string"}
        added.append(name)
        if name in in_path and name not in required:
            required.append(name)
    out["properties"] = properties
    if required:
        out["required"] = required
    return out, added
