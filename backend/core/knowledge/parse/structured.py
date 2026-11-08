"""XML and YAML, rendered as `path: value` lines the way JSON is (`text.render_json`).

Both can describe more than they hold: YAML aliases point many times at one
node (a billion-laughs document is a few hundred bytes), and an alias can
point at its own ancestor. The renderer walks a node budget and refuses to
re-enter a node it is inside, so neither expands without bound — what is cut
is a `truncated_size` warning. XML goes through defusedxml: no DTD, no entity,
no external fetch.

XML: an element's text at its path (`order/customer/name: Ada`), its
attributes as `@name`, repeated siblings numbered. Each top-level child is a
section (one block).
"""
from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import Element

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.limits import ParseRefused
from core.knowledge.parse.tree import MAX_TEXT_CHARS, DocBuilder
from core.knowledge.parse.xmlread import local, parse
from core.knowledge.parsed import ParsedDocument, ParserInfo

XML_PARSER = ParserInfo(name="xml", version="1")
YAML_PARSER = ParserInfo(name="yaml", version="1")
#: Nodes rendered per document at most.
MAX_NODES = 200_000


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0
        self.chars = 0
        self.cut = False

    def spend(self, line: str) -> bool:
        self.nodes += 1
        self.chars += len(line) + 1
        if self.nodes > MAX_NODES or self.chars > MAX_TEXT_CHARS:
            self.cut = True
        return not self.cut


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_value(value: Any, prefix: str, out: list[str], budget: _Budget, inside: tuple[int, ...] = ()) -> None:
    if budget.cut:
        return
    if isinstance(value, (dict, list)):
        if id(value) in inside:
            budget.spend(f"{prefix}: (refers to itself)")
            out.append(f"{prefix}: (refers to itself)")
            return
        inside = (*inside, id(value))
    if isinstance(value, dict):
        if not value and budget.spend(f"{prefix}: {{}}"):
            out.append(f"{prefix}: {{}}")
        for key, inner in value.items():
            render_value(inner, f"{prefix}.{key}" if prefix else str(key), out, budget, inside)
    elif isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            line = f"{prefix}: {', '.join(_scalar(v) for v in value)}"
            if budget.spend(line):
                out.append(line)
        else:
            for k, inner in enumerate(value):
                render_value(inner, f"{prefix}[{k}]", out, budget, inside)
    else:
        line = f"{prefix}: {_scalar(value)}" if prefix else _scalar(value)
        if budget.spend(line):
            out.append(line)


def _sections(items: list[tuple[str, Any]], render) -> tuple[DocBuilder, _Budget]:
    builder, budget = DocBuilder(), _Budget()
    for name, value in items:
        lines: list[str] = []
        render(value, name, lines, budget)
        if lines:
            builder.add("paragraph", "\n".join(lines))
        if budget.cut:
            builder.cut("truncated_size", "The document was cut: it describes more than one document may hold.")
            break
    return builder, budget


def read_yaml(data: bytes, ctx: ReadContext) -> ParsedDocument:
    import yaml

    from core.knowledge.parse import decode

    raw, warnings = decode(data)
    try:
        documents = [d for d in yaml.safe_load_all(raw) if d is not None]
    except yaml.YAMLError as exc:
        raise ParseRefused(f"The YAML does not parse: {str(exc).splitlines()[0]}") from exc
    items: list[tuple[str, Any]] = []
    for k, doc in enumerate(documents):
        prefix = f"[{k}]" if len(documents) > 1 else ""
        if isinstance(doc, dict):
            items.extend((f"{prefix}.{key}" if prefix else str(key), value) for key, value in doc.items())
        else:
            items.append((prefix, doc))
    builder, _ = _sections(items, render_value)
    builder.warnings.extend(warnings)
    return builder.build(ctx.source, YAML_PARSER)


def _render_xml(el: Element, prefix: str, out: list[str], budget: _Budget, inside: tuple = ()) -> None:
    if budget.cut:
        return
    for key, value in el.attrib.items():
        line = f"{prefix}@{local(key)}: {value}"
        if budget.spend(line):
            out.append(line)
    text = (el.text or "").strip()
    if text:
        line = f"{prefix}: {' '.join(text.split())}"
        if budget.spend(line):
            out.append(line)
    counts: dict[str, int] = {}
    for c in el:
        counts[local(c.tag)] = counts.get(local(c.tag), 0) + 1
    seen: dict[str, int] = {}
    for c in el:
        name = local(c.tag)
        if not name:
            continue  # a comment or processing instruction
        seen[name] = seen.get(name, 0) + 1
        path = f"{prefix}/{name}" + (f"[{seen[name]}]" if counts[name] > 1 else "")
        _render_xml(c, path, out, budget)
        tail = (c.tail or "").strip()
        if tail and budget.spend(tail):
            out.append(f"{prefix}: {' '.join(tail.split())}")


def read_xml(data: bytes, ctx: ReadContext) -> ParsedDocument:
    root = parse(data, part="XML")
    name = local(root.tag)
    items = [(f"{name}/{local(c.tag)}", c) for c in root if local(c.tag)] or [(name, root)]
    if root.attrib or (root.text or "").strip():
        head = Element(root.tag, root.attrib)
        head.text = root.text
        items.insert(0, (name, head))
    builder, _ = _sections(items, _render_xml)
    return builder.build(ctx.source, XML_PARSER)
