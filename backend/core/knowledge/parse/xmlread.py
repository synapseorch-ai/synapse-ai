"""Reading Office and OpenDocument XML safely and without caring which namespace URI a file uses.

Every part is parsed through defusedxml (no DTD, no entity expansion, no
external fetch). Elements and attributes are matched by local name, so
Transitional and Strict OOXML read alike, as do OpenDocument versions.
"""
from __future__ import annotations

from collections.abc import Iterator
from xml.etree.ElementTree import Element

from core.knowledge.parse.limits import ParseRefused


def parse(xml: bytes | str, *, part: str = "a part") -> Element:
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import ParseError, fromstring

    try:
        return fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise ParseRefused(f"The file's {part} uses XML features that are refused ({type(exc).__name__}).") from exc
    except ParseError as exc:
        raise ParseRefused(f"The file's {part} is damaged: {exc}.") from exc


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def attr(el: Element, name: str, default: str | None = None) -> str | None:
    """An attribute by local name, whatever its namespace."""
    for key, value in el.attrib.items():
        if local(key) == name:
            return value
    return default


def relationship_id(el: Element) -> str | None:
    """`r:id`: the namespaced `id`, not a plain `id` beside it (`<p:sldId id="256" r:id="rId2">`)."""
    for key, value in el.attrib.items():
        if key.startswith("{") and local(key) == "id":
            return value
    return None


def child(el: Element, name: str) -> Element | None:
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el: Element, name: str) -> list[Element]:
    return [c for c in el if local(c.tag) == name]


def find(el: Element, *path: str) -> Element | None:
    """The first element down `path` (local names), each step a direct child."""
    current: Element | None = el
    for name in path:
        if current is None:
            return None
        current = child(current, name)
    return current


def descendants(el: Element, name: str) -> Iterator[Element]:
    for d in el.iter():
        if local(d.tag) == name:
            yield d


def is_true(value: str | None, *, default: bool = True) -> bool:
    """An OOXML on/off value: absent means `default`; "0", "false", "off" mean off."""
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "off", "none")
