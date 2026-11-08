"""EPUB: the book's chapters in spine order, each through the web page extractor (`html.py`).

`META-INF/container.xml` names the package document; its manifest and spine
give the chapters in reading order. A book whose content documents are listed
in `META-INF/encryption.xml` is DRM-protected and refused as `encrypted`;
obfuscated fonts (listed there too) are not content and do not count.
"""
from __future__ import annotations

import posixpath

from core.knowledge.parse.formats import ReadContext
from core.knowledge.parse.html import HtmlExtract, html_to_markdown, markdown_document
from core.knowledge.parse.limits import Encrypted, ParseRefused
from core.knowledge.parse.xmlread import attr, children, descendants, find, local, parse
from core.knowledge.parse.zipguard import SafeZip
from core.knowledge.parsed import ParsedDocument, ParserInfo

PARSER = ParserInfo(name="epub", version="1")
_CONTENT_TYPES = ("application/xhtml+xml", "text/html", "application/x-dtbook+xml")


def read_epub(data: bytes, ctx: ReadContext) -> ParsedDocument:
    z = SafeZip(data, kind="EPUB book")
    if z.has("META-INF/encryption.xml"):
        encrypted = [attr(ref, "URI") or "" for ref in descendants(parse(z.read("META-INF/encryption.xml"),
                                                                         part="encryption"), "CipherReference")]
        if any(not uri.lower().endswith((".ttf", ".otf", ".woff", ".woff2")) for uri in encrypted):
            raise Encrypted("The book is protected by DRM.")
    container = parse(z.read("META-INF/container.xml"), part="container") if z.has("META-INF/container.xml") \
        else None
    rootfile = next(descendants(container, "rootfile"), None) if container is not None else None
    opf_path = attr(rootfile, "full-path") if rootfile is not None else None
    if not opf_path or not z.has(opf_path):
        raise ParseRefused("The book has no package document (META-INF/container.xml).")
    opf = parse(z.read(opf_path), part="package")
    folder = posixpath.dirname(opf_path)
    manifest = {attr(item, "id"): (attr(item, "href") or "", attr(item, "media-type") or "")
                for item in descendants(opf, "item")}
    spine = find(opf, "spine")
    chapters = []
    for ref in children(spine, "itemref") if spine is not None else []:
        href, media = manifest.get(attr(ref, "idref"), ("", ""))
        path = posixpath.normpath(posixpath.join(folder, href.split("#")[0])) if href else ""
        if media in _CONTENT_TYPES and z.has(path):
            chapters.append(path)

    parts, hidden, comments = [], 0, 0
    for path in chapters:
        extract = html_to_markdown(z.read(path))
        hidden += extract.hidden_removed
        comments += extract.comments_removed
        if extract.markdown.strip():
            parts.append(extract.markdown)
    meta = {local(el.tag): (el.text or "").strip() for el in (find(opf, "metadata") or [])}
    combined = HtmlExtract(markdown="\n\n".join(parts), title=meta.get("title") or None,
                           language=meta.get("language") or None,
                           published=meta.get("date", "")[:10] if len(meta.get("date", "")) >= 10 else None,
                           hidden_removed=hidden, comments_removed=comments)
    return markdown_document(combined, ctx.source, PARSER, [])
