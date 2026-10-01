"""Real files of every format Knowledge reads, built in the test (A2, D162).

Nothing here is committed binary: each builder writes the format's own
structure (PDF syntax, OOXML / ODF / EPUB zips, an OLE container for .msg), so
a test states exactly what the file holds — a heading at 20 pt, a white line,
a hidden sheet — and asserts what the reader makes of it.
"""
from __future__ import annotations

import io
import zipfile
import zlib
from dataclasses import dataclass, field

# ── PDF ────────────────────────────────────────────────────────────────────────


@dataclass
class PdfPage:
    """Drawing operations for one page, in PDF points with the origin at the bottom left (PDF's own)."""

    ops: list[bytes] = field(default_factory=list)
    width: float = 612
    height: float = 792

    def text(self, x: float, y: float, s: str, *, size: float = 11, bold: bool = False,
             gray: float | None = None, rgb: tuple[float, float, float] | None = None) -> PdfPage:
        colour = b""
        if gray is not None:
            colour = f"{gray} g ".encode()
        elif rgb is not None:
            colour = f"{rgb[0]} {rgb[1]} {rgb[2]} rg ".encode()
        escaped = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")
        font = b"/F2" if bold else b"/F1"
        self.ops.append(b"q " + colour + b"BT " + font + f" {size} Tf {x} {y} Td (".encode() + escaped
                        + b") Tj ET Q")
        return self

    def lines(self, x: float, y: float, rows: list[str], *, size: float = 11, leading: float | None = None,
              **kw) -> PdfPage:
        step = leading or size * 1.3
        for k, row in enumerate(rows):
            self.text(x, y - k * step, row, size=size, **kw)
        return self

    def rect(self, x: float, y: float, w: float, h: float, *, fill_gray: float | None = None,
             stroke: bool = False) -> PdfPage:
        op = f"q {fill_gray} g {x} {y} {w} {h} re f Q" if fill_gray is not None else ""
        if stroke:
            op += f" q 0.5 w {x} {y} {w} {h} re S Q"
        self.ops.append(op.strip().encode())
        return self

    def hline(self, x0: float, x1: float, y: float) -> PdfPage:
        self.ops.append(f"q 0.5 w {x0} {y} m {x1} {y} l S Q".encode())
        return self

    def vline(self, x: float, y0: float, y1: float) -> PdfPage:
        self.ops.append(f"q 0.5 w {x} {y0} m {x} {y1} l S Q".encode())
        return self

    def image(self, x: float = 100, y: float = 200, w: float = 400, h: float = 400) -> PdfPage:
        self.ops.append(f"q {w} 0 0 {h} {x} {y} cm /Im1 Do Q".encode())
        return self

    def grid(self, x: float, top: float, rows: list[list[str]], *, col_w: float = 120,
             row_h: float = 20, size: float = 10) -> PdfPage:
        """A ruled table: every cell boxed, text inside."""
        n_cols = max(len(r) for r in rows)
        bottom = top - row_h * len(rows)
        for k in range(len(rows) + 1):
            self.hline(x, x + col_w * n_cols, top - k * row_h)
        for c in range(n_cols + 1):
            self.vline(x + c * col_w, bottom, top)
        for k, row in enumerate(rows):
            for c, cell in enumerate(row):
                self.text(x + c * col_w + 4, top - (k + 1) * row_h + 6, cell, size=size)
        return self


def pdf(pages: list[PdfPage], *, title: str | None = None, encrypt_with: str | None = None) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    image_data = zlib.compress(bytes([(x * 7 + y * 13) % 256 for y in range(32) for x in range(32)]))
    image = add(b"<< /Type /XObject /Subtype /Image /Width 32 /Height 32 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(image_data)).encode()
                + b" >>\nstream\n" + image_data + b"\nendstream")
    pages_index = len(objects) + 1 + 2 * len(pages)
    kids = []
    for page in pages:
        content = b"\n".join(page.ops)
        stream = add(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent " + str(pages_index).encode() + b" 0 R "
            + f"/MediaBox [0 0 {page.width} {page.height}] ".encode()
            + b"/Resources << /Font << /F1 " + str(regular).encode() + b" 0 R /F2 " + str(bold).encode()
            + b" 0 R >> /XObject << /Im1 " + str(image).encode() + b" 0 R >> >> /Contents "
            + str(stream).encode() + b" 0 R >>"))
    pages_obj = add(b"<< /Type /Pages /Kids [" + b" ".join(f"{k} 0 R".encode() for k in kids) + b"] /Count "
                    + str(len(kids)).encode() + b" >>")
    assert pages_obj == pages_index
    catalog = add(b"<< /Type /Catalog /Pages " + str(pages_obj).encode() + b" 0 R >>")
    info = add(b"<< /Title (" + title.encode("latin-1") + b") /CreationDate (D:20250314093000Z) >>") if title else None
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objects) + 1} /Root {catalog} 0 R" + (f" /Info {info} 0 R" if info else "") + " >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref}\n%%EOF\n".encode()
    data = bytes(out)
    if encrypt_with is not None:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        writer.encrypt(user_password=encrypt_with, algorithm="RC4-128")
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
    return data


def text_page(*paragraphs: str, size: float = 11) -> PdfPage:
    """A page of body paragraphs, a blank line apart."""
    page = PdfPage()
    y = 720.0
    for paragraph in paragraphs:
        page.text(72, y, paragraph, size=size)
        y -= size * 2.6
    return page


# ── zips ───────────────────────────────────────────────────────────────────────

def zipped(members: dict[str, bytes | str], *, first: str | None = None) -> bytes:
    """A zip; `first` is stored uncompressed at the front (ODF's and EPUB's `mimetype`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if first is not None:
            zf.writestr(zipfile.ZipInfo("mimetype"), members[first], compress_type=zipfile.ZIP_STORED)
        for name, body in members.items():
            if name != first:
                zf.writestr(name, body)
    return buf.getvalue()


# ── Word ───────────────────────────────────────────────────────────────────────

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
WORD_STYLES = (
    f'<w:styles {W}>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Secret"><w:name w:val="Secret"/><w:rPr><w:vanish/></w:rPr></w:style>'
    '<w:style w:type="character" w:styleId="Ghost"><w:name w:val="Ghost"/><w:rPr><w:color w:val="FFFFFF"/></w:rPr>'
    '</w:style></w:styles>')


def w_run(text: str, props: str = "") -> str:
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def w_p(*runs: str, style: str | None = None, numbered: bool = False) -> str:
    ppr = ""
    if style or numbered:
        ppr = ("<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "")
               + ('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if numbered else "") + "</w:pPr>")
    body = "".join(r if r.startswith("<") else w_run(r) for r in runs)
    return f"<w:p>{ppr}{body}</w:p>"


def w_table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{w_p(c)}</w:tc>" for c in row) + "</w:tr>"
                               for row in rows) + "</w:tbl>"


def docx(body: str, *, styles: str = WORD_STYLES, title: str | None = None, created: str | None = None,
         footnotes: list[str] | None = None) -> bytes:
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>",
        "word/styles.xml": styles,
    }
    if title or created:
        members["docProps/core.xml"] = (
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
            + (f"<dc:title>{title}</dc:title>" if title else "")
            + (f"<dcterms:created>{created}</dcterms:created>" if created else "") + "</cp:coreProperties>")
    if footnotes:
        members["word/footnotes.xml"] = (
            f'<w:footnotes {W}><w:footnote w:type="separator" w:id="-1">{w_p("———")}</w:footnote>'
            + "".join(f'<w:footnote w:id="{k}">{w_p(t)}</w:footnote>' for k, t in enumerate(footnotes, 1))
            + "</w:footnotes>")
    return zipped(members)


# ── PowerPoint ─────────────────────────────────────────────────────────────────

P_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def p_shape(paragraphs: list[str], *, ph: str | None = None, hidden: bool = False, size: int | None = None) -> str:
    rpr = f'<a:rPr sz="{size}"/>' if size else ""
    body = "".join(f"<a:p><a:r>{rpr}<a:t>{t}</a:t></a:r></a:p>" for t in paragraphs)
    return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"' + (' hidden="1"' if hidden else "") + "/><p:cNvSpPr/>"
            + (f'<p:nvPr><p:ph type="{ph}"/></p:nvPr>' if ph else "<p:nvPr/>")
            + f"</p:nvSpPr><p:txBody>{body}</p:txBody></p:sp>")


def p_table(rows: list[list[str]]) -> str:
    cells = "".join("<a:tr>" + "".join(f"<a:tc><a:txBody><a:p><a:r><a:t>{c}</a:t></a:r></a:p></a:txBody></a:tc>"
                                       for c in row) + "</a:tr>" for row in rows)
    return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="t"/></p:nvGraphicFramePr>'
            f"<a:graphic><a:graphicData><a:tbl>{cells}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")


def pptx(slides: list[str], *, notes: dict[int, str] | None = None, hidden: set[int] = frozenset(),
         title: str | None = None) -> bytes:
    notes = notes or {}
    ids = "".join(f'<p:sldId id="{255 + k}" r:id="rId{k}"/>' for k in range(1, len(slides) + 1))
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "ppt/presentation.xml": f"<p:presentation {P_NS}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": f"<Relationships {RELS_NS}>" + "".join(
            f'<Relationship Id="rId{k}" Type="slide" Target="slides/slide{k}.xml"/>'
            for k in range(1, len(slides) + 1)) + "</Relationships>",
    }
    for k, shapes in enumerate(slides, start=1):
        show = ' show="0"' if k in hidden else ""
        members[f"ppt/slides/slide{k}.xml"] = (f"<p:sld {P_NS}{show}><p:cSld><p:spTree>{shapes}"
                                               "</p:spTree></p:cSld></p:sld>")
        if k in notes:
            members[f"ppt/slides/_rels/slide{k}.xml.rels"] = (
                f'<Relationships {RELS_NS}><Relationship Id="rId1" Type="notesSlide" '
                f'Target="../notesSlides/notesSlide{k}.xml"/></Relationships>')
            members[f"ppt/notesSlides/notesSlide{k}.xml"] = (
                f"<p:notes {P_NS}><p:cSld><p:spTree>{p_shape(['1'], ph='sldNum')}{p_shape([notes[k]], ph='body')}"
                "</p:spTree></p:cSld></p:notes>")
    if title:
        members["docProps/core.xml"] = ('<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                                        f"<dc:title>{title}</dc:title></cp:coreProperties>")
    return zipped(members)


# ── OpenDocument ───────────────────────────────────────────────────────────────

ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
          'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
          'xmlns:dc="http://purl.org/dc/elements/1.1/"')
_ODF_KINDS = {"odt": ("text", "application/vnd.oasis.opendocument.text"),
              "odp": ("presentation", "application/vnd.oasis.opendocument.presentation"),
              "ods": ("spreadsheet", "application/vnd.oasis.opendocument.spreadsheet")}


def odf(kind: str, body: str, *, styles: str = "", title: str | None = None) -> bytes:
    element, mimetype = _ODF_KINDS[kind]
    members: dict[str, bytes | str] = {
        "mimetype": mimetype,
        "content.xml": (f"<office:document-content {ODF_NS}><office:automatic-styles>{styles}"
                        f"</office:automatic-styles><office:body><office:{element}>{body}</office:{element}>"
                        "</office:body></office:document-content>"),
    }
    if title:
        members["meta.xml"] = (f"<office:document-meta {ODF_NS}><office:meta><dc:title>{title}</dc:title>"
                               "<meta:creation-date>2024-05-06T10:00:00</meta:creation-date></office:meta>"
                               "</office:document-meta>")
    return zipped(members, first="mimetype")


# ── OLE compound files and Outlook .msg ────────────────────────────────────────

_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF


def ole_file(streams: dict[tuple[str, ...], bytes]) -> bytes:
    """A version-3 compound file (MS-CFB) holding `streams` at their storage paths.

    Small streams live in the mini stream, as the format requires (olefile reads
    them from there); storages are implied by the paths. Siblings are kept as a
    right-leaning chain in the format's name order, which readers accept.
    """
    import struct as st

    # The directory: a tree of storages with their children.
    tree: dict[tuple[str, ...], dict] = {(): {"kind": 5, "children": []}}
    for path, data in sorted(streams.items()):
        for depth in range(1, len(path)):
            node = path[:depth]
            if node not in tree:
                tree[node] = {"kind": 1, "children": []}
                tree[path[:depth - 1]]["children"].append(node)
        tree[path] = {"kind": 2, "data": data}
        tree[path[:-1]]["children"].append(path)
    order = [()] + [p for p in tree if p != ()]
    index = {p: k for k, p in enumerate(order)}

    def name_key(path):
        return (len(path[-1]), path[-1].upper())

    small = {p: e["data"] for p, e in tree.items() if e["kind"] == 2 and len(e["data"]) < 4096}
    big = {p: e["data"] for p, e in tree.items() if e["kind"] == 2 and len(e["data"]) >= 4096}
    sectors: list[bytes] = []
    fat: list[int] = []

    def chain(data: bytes, size: int = 512) -> int:
        count = max(1, -(-len(data) // size))
        first = len(sectors)
        for k in range(count):
            sectors.append(data[k * size:(k + 1) * size].ljust(size, b"\0"))
            fat.append(first + k + 1 if k < count - 1 else _END)
        return first

    start: dict[tuple, int] = {}
    for path, data in big.items():
        start[path] = chain(data)
    mini, minifat = bytearray(), []
    for path, data in small.items():
        count = max(1, -(-len(data) // 64))
        first = len(mini) // 64
        mini += data.ljust(count * 64, b"\0")
        minifat += [first + k + 1 if k < count - 1 else _END for k in range(count)]
        start[path] = first
    mini_start = chain(bytes(mini)) if mini else _END
    minifat_start = chain(b"".join(st.pack("<I", v) for v in minifat).ljust(512 * max(1, -(-len(minifat) // 128)),
                                                                          b"\xff")) if minifat else _END
    n_minifat = -(-len(minifat) // 128) if minifat else 0

    entries = []
    for path in order:
        entry = tree[path]
        name = "Root Entry" if path == () else path[-1]
        encoded = (name + "\0").encode("utf-16-le")
        siblings = sorted(tree[path[:-1]]["children"], key=name_key) if path else []
        right = siblings[siblings.index(path) + 1] if path and siblings.index(path) + 1 < len(siblings) else None
        kids = sorted(entry.get("children", []), key=name_key)
        if entry["kind"] == 5:
            sect, size = mini_start, len(mini)
        elif entry["kind"] == 2:
            sect, size = start[path], len(entry["data"])
        else:
            sect, size = 0, 0
        entries.append(encoded.ljust(64, b"\0") + st.pack("<HBB", len(encoded), entry["kind"], 1)
                       + st.pack("<III", _NOSTREAM, index[right] if right else _NOSTREAM,
                                 index[kids[0]] if kids else _NOSTREAM)
                       + b"\0" * 16 + st.pack("<I", 0) + b"\0" * 16 + st.pack("<IQ", sect, size))
    directory = b"".join(entries)
    dir_start = chain(directory.ljust(512 * max(1, -(-len(directory) // 512)), b"\0"))

    n_fat = 1
    while (len(sectors) + n_fat) > 128 * n_fat:
        n_fat += 1
    fat_start = len(sectors)
    fat += [_FAT] * n_fat
    fat += [_FREE] * (128 * n_fat - len(fat))
    for k in range(n_fat):
        sectors.append(b"".join(st.pack("<I", v) for v in fat[k * 128:(k + 1) * 128]))
    difat = [fat_start + k for k in range(n_fat)] + [_FREE] * (109 - n_fat)
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + st.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
              + b"\0" * 6 + st.pack("<IIIIIIIII", 0, n_fat, dir_start, 0, 4096, minifat_start, n_minifat, _END, 0)
              + b"".join(st.pack("<I", v) for v in difat))
    return header + b"".join(sectors)


def _prop(tag: int, value: int) -> bytes:
    import struct as st

    return st.pack("<IIQ", tag, 6, value)


def msg(subject: str, *, body: str | None = None, html: str | None = None, sender: str = "Ada",
        address: str = "ada@example.com", to: str = "Ops", sent: tuple[int, int, int] | None = (2025, 3, 4),
        attachments: list[tuple[str, str, bytes]] = (), inline_image: bool = False,
        embedded: list[tuple[str, str, str]] = ()) -> bytes:
    """An Outlook message: `attachments` as (name, mime, data); `embedded` as (name, subject, body) messages."""
    from datetime import datetime, timezone

    def text(prop: int, value: str) -> tuple[str, bytes]:
        return f"__substg1.0_{prop:04X}001F", value.encode("utf-16-le")

    def message(prefix: tuple[str, ...], subject: str, body: str | None, *, top: bool) -> dict:
        streams = {}
        for prop, value in ((0x0037, subject), (0x0C1A, sender), (0x5D01, address), (0x0E04, to)):
            name, data = text(prop, value)
            streams[(*prefix, name)] = data
        if body is not None:
            name, data = text(0x1000, body)
            streams[(*prefix, name)] = data
        props = b"\0" * (32 if top else 24)
        if sent:
            delta = datetime(*sent, 9, 30, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)
            props += _prop((0x0039 << 16) | 0x0040, int(delta.total_seconds() * 10_000_000))
        streams[(*prefix, "__properties_version1.0")] = props
        return streams

    streams = message((), subject, body, top=True)
    if html is not None:
        streams[("__substg1.0_10130102",)] = html.encode("utf-8")
    parts = list(attachments)
    if inline_image:
        parts.append(("logo.png", "image/png", b"\x89PNG\r\n\x1a\n"))
    for k, (name, mime, data) in enumerate(parts):
        store = f"__attach_version1.0_#{k:08X}"
        streams[(store, "__substg1.0_3707001F")] = name.encode("utf-16-le")
        streams[(store, "__substg1.0_370E001F")] = mime.encode("utf-16-le")
        streams[(store, "__substg1.0_37010102")] = data
        if inline_image and name == "logo.png":
            streams[(store, "__substg1.0_3712001F")] = "logo@cid".encode("utf-16-le")
        streams[(store, "__properties_version1.0")] = b"\0" * 8
    for k, (name, subject_, body_) in enumerate(embedded, start=len(parts)):
        store = f"__attach_version1.0_#{k:08X}"
        streams[(store, "__substg1.0_3707001F")] = name.encode("utf-16-le")
        streams[(store, "__properties_version1.0")] = b"\0" * 8
        streams.update(message((store, "__substg1.0_3701000D"), subject_, body_, top=False))
    return ole_file(streams)


def flat_odf(kind: str, body: str) -> bytes:
    """A flat OpenDocument file (`.fodt` / `.fodp`): one XML file LibreOffice opens directly. Tests hand it
    to LibreOffice to write a 97–2003 file, since a hand-built OOXML zip lacks the parts LibreOffice wants."""
    element, mimetype = _ODF_KINDS[kind]
    return (f'<?xml version="1.0" encoding="UTF-8"?><office:document {ODF_NS} '
            'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3" '
            f'office:mimetype="{mimetype}"><office:body><office:{element}>{body}</office:{element}>'
            "</office:body></office:document>").encode()


FODT_MEMO = ('<text:h text:outline-level="1">Quarterly memo</text:h>'
             "<text:p>Revenue rose four percent.</text:p>")
FODP_DECK = ('<draw:page draw:name="p1"><draw:frame presentation:class="title" svg:x="1cm" svg:y="1cm" '
             'svg:width="20cm" svg:height="3cm"><draw:text-box><text:p>Kickoff</text:p></draw:text-box></draw:frame>'
             '<draw:frame svg:x="1cm" svg:y="5cm" svg:width="20cm" svg:height="10cm"><draw:text-box>'
             "<text:p>Goals</text:p><text:p>Owners</text:p></draw:text-box></draw:frame></draw:page>")
