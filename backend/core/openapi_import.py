"""
Convert an OpenAPI 3.x / Swagger 2.0 spec into Synapse custom-tool dicts.

Each spec operation (path + HTTP method) becomes one HTTP custom tool that plugs
directly into the existing executor in react_engine.py:
  - path parameters stay as ``{param}`` placeholders in the URL — the executor
    substitutes them from the call arguments;
  - query parameters and the JSON request body are described in ``inputSchema`` so
    the LLM supplies them (the executor routes them to query params for GET/DELETE
    or to the JSON body for POST/PUT).

Header parameters are intentionally NOT surfaced as arguments: the executor cannot
route an argument into a request header, so exposing them would mis-send them as
query/body fields. Supply auth or other static headers via the ``headers`` argument
(e.g. an ``Authorization`` header entered once at import time).
"""
import re
from typing import Any

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _sanitize_name(raw: str) -> str:
    """Reduce an operationId / path to a snake_case tool name (alnum + underscore)."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", raw or "").strip("_").lower()
    if not s:
        s = "tool"
    if s[0].isdigit():
        s = f"op_{s}"
    return s


def _resolve_ref(spec: dict, node: Any, _seen: frozenset | None = None) -> Any:
    """Recursively resolve local ``$ref`` pointers (``#/...``) within a schema node.

    External refs (URLs / file paths) can't be resolved from the spec alone, so
    they collapse to ``{}``. A cycle guard prevents infinite recursion on
    self-referential schemas.
    """
    if _seen is None:
        _seen = frozenset()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/") or ref in _seen:
                return {}
            target: Any = spec
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(target, dict) and part in target:
                    target = target[part]
                else:
                    return {}
            return _resolve_ref(spec, target, _seen | {ref})
        return {k: _resolve_ref(spec, v, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_ref(spec, v, _seen) for v in node]
    return node


def _base_url_from_spec(spec: dict, override: str | None) -> str:
    """Resolve the server base URL: explicit override → OpenAPI 3.x → Swagger 2.0."""
    if override:
        return override.rstrip("/")
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        url = (servers[0] or {}).get("url", "")
        if url:
            return url.rstrip("/")
    host = spec.get("host")  # Swagger 2.0
    if host:
        schemes = spec.get("schemes") or ["https"]
        scheme = schemes[0] if schemes else "https"
        base_path = spec.get("basePath", "") or ""
        return f"{scheme}://{host}{base_path}".rstrip("/")
    return ""


def _param_schema(spec: dict, param: dict) -> dict:
    """Extract the JSON-Schema for a single parameter (3.x ``schema`` or 2.0 inline)."""
    if "schema" in param:
        sch = _resolve_ref(spec, param["schema"])
    else:  # Swagger 2.0 puts type fields directly on the parameter
        sch = {k: param[k] for k in ("type", "format", "items", "enum", "default")
               if k in param}
    if not isinstance(sch, dict) or not sch:
        sch = {"type": "string"}
    if param.get("description") and "description" not in sch:
        sch = {**sch, "description": param["description"]}
    return sch


def _build_input_schema(spec: dict, shared_params: list, op: dict) -> dict:
    """Build a JSON-Schema object from an operation's path/query params + body."""
    props: dict = {}
    required: list = []
    params = [_resolve_ref(spec, p) for p in (list(shared_params) + list(op.get("parameters", [])))]

    for p in params:
        if not isinstance(p, dict):
            continue
        loc = p.get("in")
        name = p.get("name")
        if loc not in ("path", "query") or not name:
            continue  # header/cookie params can't be routed by the executor — skip
        props[name] = _param_schema(spec, p)
        if (p.get("required") or loc == "path") and name not in required:
            required.append(name)

    # Request body: OpenAPI 3.x (requestBody.content) then Swagger 2.0 (in: body).
    def _merge_body(body_schema: dict, body_required: bool):
        body_schema = _resolve_ref(spec, body_schema)
        if not isinstance(body_schema, dict) or not body_schema:
            return
        if "properties" in body_schema:
            for k, v in (body_schema.get("properties") or {}).items():
                props[k] = v
            for r in body_schema.get("required", []) or []:
                if r not in required:
                    required.append(r)
        else:  # non-object body (array/scalar) — expose under a single 'body' field
            props["body"] = body_schema
            if body_required and "body" not in required:
                required.append("body")

    request_body = _resolve_ref(spec, op.get("requestBody", {})) if op.get("requestBody") else {}
    if request_body:
        content = request_body.get("content", {}) or {}
        media = content.get("application/json") or (next(iter(content.values()), {}) if content else {})
        _merge_body(media.get("schema", {}), bool(request_body.get("required")))

    for p in params:  # Swagger 2.0 body parameter
        if isinstance(p, dict) and p.get("in") == "body":
            _merge_body(p.get("schema", {}), bool(p.get("required")))

    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def parse_openapi_spec(
    spec: dict,
    *,
    base_url: str | None = None,
    headers: dict | None = None,
    name_prefix: str = "",
) -> list[dict]:
    """Convert an OpenAPI 3.x / Swagger 2.0 spec into a list of custom-tool dicts.

    Args:
        spec: the parsed spec (a dict, from JSON or YAML).
        base_url: override for the server base URL (wins over ``servers``/``host``).
        headers: static headers applied to every generated tool (e.g. auth).
        name_prefix: prepended (sanitized) to every generated tool name.

    Returns tool dicts in the same shape stored by ``POST /api/tools/custom``.
    Raises ``ValueError`` if the document has no usable operations.
    """
    if not isinstance(spec, dict):
        raise ValueError("Spec must be a JSON/YAML object.")
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("Spec has no 'paths' — not a valid OpenAPI/Swagger document.")

    base = _base_url_from_spec(spec, base_url)
    headers = dict(headers or {})
    prefix = f"{_sanitize_name(name_prefix)}_" if name_prefix else ""

    tools: list[dict] = []
    used_names: set = set()

    for path, path_item in paths.items():
        if isinstance(path_item, dict) and "$ref" in path_item:
            path_item = _resolve_ref(spec, path_item)
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", []) or []

        for method, op in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue

            op_id = op.get("operationId")
            name = prefix + _sanitize_name(op_id if op_id else f"{method}_{path}")
            base_name, i = name, 2
            while name in used_names:
                name, i = f"{base_name}_{i}", i + 1
            used_names.add(name)

            summary = (op.get("summary") or "").strip()
            description = (summary or op.get("description") or "").strip() or f"{method.upper()} {path}"

            tools.append({
                "name": name,
                "generalName": (summary or name)[:80],
                "description": description,
                "tool_type": "http",
                "method": method.upper(),
                "url": f"{base}{path}",
                "headers": dict(headers),
                "inputSchema": _build_input_schema(spec, shared_params, op),
            })

    if not tools:
        raise ValueError("No operations found in the spec.")
    return tools
