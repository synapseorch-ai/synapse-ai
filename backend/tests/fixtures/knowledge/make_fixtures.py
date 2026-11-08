"""Regenerate Knowledge's committed fixtures: `python backend/tests/fixtures/knowledge/make_fixtures.py`.

Everything here is synthetic — written for these tests, no customer data, no
third-party text. Naveen's real 20–50 document corpus is never committed; it
lives outside both repositories at `$SYNAPSE_KB_CORPUS` (D153).

* `parsed/*.json` — six `ParsedDocument`s, one per source shape (a PDF with a
  table and a page with no text layer, a DOCX, a two-sheet XLSX, an email with
  an attachment, a web page, a record). Offsets are computed, so they satisfy
  the validator by construction.
* `corpus/` — small real files for parser tests: markdown, text, CSV, a JSON
  record, an XLSX (openpyxl), a PDF with a text layer and one without (PDF
  bytes written by hand: no PDF library is a dependency), a DOCX (OOXML written
  by hand) and an .eml with an attachment. A legacy `.doc` cannot be written
  without LibreOffice; A2 converts one in its own tests.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
import zlib
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # backend/

from core.knowledge.parsed import ParsedDocument  # noqa: E402

NOW = "2026-09-30T00:00:00+00:00"


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def build(source: dict, parser: dict, blocks: list[dict], *, pages: list[dict] | None = None, **extra) -> dict:
    """A ParsedDocument dict whose offsets are right by construction."""
    text_parts: list[str] = []
    cursor = 0
    out_blocks = []
    for n, b in enumerate(blocks):
        if text_parts:
            text_parts.append("\n\n")
            cursor += 2
        start = cursor
        text_parts.append(b["text"])
        cursor += len(b["text"])
        block = {"id": b.get("id") or f"b{(b.get('page') or 0):04d}-{n:04d}", "char_start": start,
                 "char_end": cursor, **{k: v for k, v in b.items() if k != "id"}}
        out_blocks.append(block)
    text = "".join(text_parts)
    doc = {"schema_version": 1, "source": source, "parser": parser, "text": text, "blocks": out_blocks,
           "pages": pages or [], **extra}
    stats = doc.setdefault("stats", {})
    stats.setdefault("pages", len(doc["pages"]))
    stats.setdefault("chars", len(text))
    ParsedDocument.model_validate(doc)  # fails loudly if a fixture is wrong
    return doc


def parsed_fixtures() -> dict[str, dict]:
    docling = {"name": "docling", "version": "2.x", "options_hash": "fixture"}
    pdf = build(
        {"path": "contracts/acme-msa.pdf", "sha256": _sha("acme-msa"), "size_bytes": 48213,
         "mime": "application/pdf", "source": "upload"},
        docling,
        [
            {"kind": "heading", "level": 1, "text": "Master Services Agreement", "page": 1,
             "heading_path": ["Master Services Agreement"]},
            {"kind": "paragraph", "page": 1, "heading_path": ["Master Services Agreement"],
             "text": "This agreement is made between Acme Ltd and Example Corp for data processing services."},
            {"kind": "heading", "level": 2, "text": "Fees", "page": 2,
             "heading_path": ["Master Services Agreement", "Fees"]},
            {"kind": "table", "page": 2, "heading_path": ["Master Services Agreement", "Fees"],
             "text": "Service | Monthly fee\nHosting | 1,200\nSupport | 300",
             "table": {"rows": [["Service", "Monthly fee"], ["Hosting", "1,200"], ["Support", "300"]],
                       "header_rows": 1, "n_rows": 3, "n_cols": 2, "caption": "Fees"},
             "bbox": [{"page": 2, "x0": 72, "y0": 140, "x1": 520, "y1": 220}]},
            {"kind": "footnote", "page": 2, "heading_path": ["Master Services Agreement", "Fees"],
             "text": "Fees exclude VAT.", "furniture": False},
        ],
        pages=[
            {"number": 1, "width": 612, "height": 792, "text_chars": 110},
            {"number": 2, "width": 612, "height": 792, "text_chars": 70},
            {"number": 3, "width": 612, "height": 792, "text_chars": 0,
             "quality": {"no_text_layer": True, "image_only": True}, "warnings": ["no_text_layer"]},
        ],
        title="Master Services Agreement", doc_date="2024-03-01", language="en",
        warnings=[{"code": "no_text_layer", "message": "Page 3 has no text layer and was not read (no OCR).",
                   "page": 3}],
        stats={"pages": 3, "pages_no_text_layer": 1, "tables": 1},
    )
    docx = build(
        {"path": "contracts/zurich.docx", "sha256": _sha("zurich"), "size_bytes": 18230,
         "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "source": "upload"},
        docling,
        [
            {"kind": "heading", "level": 1, "text": "Haftpflichtvertrag", "heading_path": ["Haftpflichtvertrag"]},
            {"kind": "paragraph", "heading_path": ["Haftpflichtvertrag"],
             "text": "Dieser Vertrag regelt die Haftpflicht zwischen der Zürich Versicherung und dem Kunden."},
            {"kind": "heading", "level": 2, "text": "Kündigung", "heading_path": ["Haftpflichtvertrag", "Kündigung"]},
            {"kind": "list", "heading_path": ["Haftpflichtvertrag", "Kündigung"],
             "text": "- dreißig Tage zum Monatsende\n- schriftlich", "items": ["dreißig Tage zum Monatsende",
                                                                               "schriftlich"]},
        ],
        title="Haftpflichtvertrag", language="de", metadata={"doc_type": "contract", "region": "CH"},
    )
    xlsx = build(
        {"path": "finance/q3.xlsx", "sha256": _sha("q3"), "size_bytes": 9120,
         "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "source": "vault"},
        {"name": "basic", "version": "1", "options_hash": "fixture"},
        [
            {"id": "s1-0000", "kind": "sheet", "text": "Sheet Revenue: month, region, amount (36 rows)",
             "sheet": {"name": "Revenue", "range": "A1:C37", "n_rows": 36, "n_cols": 3,
                       "headers": ["month", "region", "amount"],
                       "sample_rows": [["2025-07", "EU", "1200"], ["2025-07", "US", "900"]],
                       "dtypes": ["date", "string", "number"]}},
            {"id": "s2-0000", "kind": "sheet", "text": "Sheet Costs: item, amount (4 rows)",
             "sheet": {"name": "Costs", "n_rows": 4, "n_cols": 2, "headers": ["item", "amount"],
                       "sample_rows": [["Hosting", "300"]]}},
        ],
        title="Q3 figures",
    )
    attachment = build(
        {"path": "mail/renewal.eml#invoice.txt", "sha256": _sha("invoice"), "size_bytes": 64,
         "mime": "text/plain", "source": "upload"},
        {"name": "basic", "version": "1"},
        [{"kind": "paragraph", "text": "Invoice 2025-117: 1,500 due 31 October."}],
    )
    eml = build(
        {"path": "mail/renewal.eml", "sha256": _sha("renewal"), "size_bytes": 2048,
         "mime": "message/rfc822", "source": "upload"},
        {"name": "eml", "version": "1"},
        [
            {"kind": "heading", "level": 1, "text": "Renewal of the Acme contract",
             "heading_path": ["Renewal of the Acme contract"]},
            {"kind": "paragraph", "heading_path": ["Renewal of the Acme contract"],
             "text": "Hi, please find the renewal invoice attached. It renews automatically on 1 November."},
        ],
        title="Renewal of the Acme contract", doc_date="2025-10-01",
        metadata={"from": "billing@example.com", "to": ["ops@example.org"]},
        attachments=[{"name": "invoice.txt", "sha256": _sha("invoice"), "mime": "text/plain", "size_bytes": 64,
                      "parsed": attachment}],
    )
    web = build(
        {"path": "_web/example.com/pricing.md", "sha256": _sha("pricing"), "size_bytes": 812, "mime": "text/html",
         "source": "web", "url": "https://example.com/pricing", "fetched_at": NOW, "etag": "\"abc123\""},
        {"name": "web", "version": "1"},
        [
            {"kind": "heading", "level": 1, "text": "Pricing", "heading_path": ["Pricing"]},
            {"kind": "paragraph", "heading_path": ["Pricing"], "text": "The team plan costs twenty dollars a seat."},
        ],
        title="Pricing",
        warnings=[{"code": "html_comments_removed", "message": "2 HTML comments were removed.", "count": 2}],
    )
    record = build(
        {"path": "_records/ticket-4411.json", "sha256": _sha("ticket"), "size_bytes": 190,
         "mime": "application/json", "source": "record"},
        {"name": "record", "version": "1"},
        [{"kind": "paragraph", "text": "Customer cannot reset password; reset link expired and was resent."}],
        title="Ticket 4411", doc_date="2025-01-20", metadata={"doc_type": "ticket", "priority": 1,
                                                                "tags": ["support"]},
    )
    return {"pdf_contract": pdf, "docx_contract": docx, "xlsx_two_sheets": xlsx, "eml_with_attachment": eml,
            "web_page": web, "record": record}


# ── the synthetic corpus ───────────────────────────────────────────────────────

def _pdf(pages: list[bytes | None]) -> bytes:
    """A minimal PDF. A page is a text line (bytes) or None for an image-only page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image_data = zlib.compress(bytes([(x * 7 + y * 13) % 256 for y in range(32) for x in range(32)]))
    image = add(b"<< /Type /XObject /Subtype /Image /Width 32 /Height 32 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(image_data)).encode()
                + b" >>\nstream\n" + image_data + b"\nendstream")
    pages_obj_index = len(objects) + 1 + 2 * len(pages)
    kids = []
    for line in pages:
        if line is None:
            content = b"q 400 0 0 400 100 200 cm /Im1 Do Q"
        else:
            content = b"BT /F1 14 Tf 72 720 Td (" + line + b") Tj ET"
        stream = add(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        page = add(b"<< /Type /Page /Parent " + str(pages_obj_index).encode() + b" 0 R /MediaBox [0 0 612 792] "
                   b"/Resources << /Font << /F1 " + str(font).encode() + b" 0 R >> /XObject << /Im1 "
                   + str(image).encode() + b" 0 R >> >> /Contents " + str(stream).encode() + b" 0 R >>")
        kids.append(page)
    pages_obj = add(b"<< /Type /Pages /Kids [" + b" ".join(f"{k} 0 R".encode() for k in kids) + b"] /Count "
                    + str(len(kids)).encode() + b" >>")
    assert pages_obj == pages_obj_index
    catalog = add(b"<< /Type /Catalog /Pages " + str(pages_obj).encode() + b" 0 R >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def _docx(paragraphs: list[str]) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def corpus_files() -> dict[str, bytes]:
    from io import BytesIO

    import openpyxl

    files: dict[str, bytes] = {
        "notes.md": b"# Onboarding notes\n\nNew customers get a kickoff call within five days.\n\n"
                    b"## Escalation\n\nSeverity 1 incidents page the on-call engineer.\n",
        "plain.txt": "Plain text with an accent (Zürich) and a zero​width space.\n".encode("utf-8"),
        "table.csv": b"month,region,amount\n2025-07,EU,1200\n2025-07,US,900\n2025-08,EU,1350\n",
        "record.json": json.dumps({"id": "ticket-4411", "text": "Customer cannot reset password.",
                                   "metadata": {"doc_type": "ticket", "priority": 1}}, indent=2).encode(),
        "digital.pdf": _pdf([b"Invoice 2025-117 for hosting services.", b"Payment is due within 30 days."]),
        "scanned.pdf": _pdf([None]),
        "contract.docx": _docx(["Service Agreement", "The supplier provides hosting for twelve months."]),
    }
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Revenue"
    ws.append(["month", "region", "amount"])
    for row in (["2025-07", "EU", 1200], ["2025-07", "US", 900], ["2025-08", "EU", 1350]):
        ws.append(row)
    costs = wb.create_sheet("Costs")
    costs.append(["item", "amount"])
    costs.append(["Hosting", 300])
    buf = BytesIO()
    wb.save(buf)
    files["small.xlsx"] = buf.getvalue()

    msg = EmailMessage()
    msg["From"] = "billing@example.com"
    msg["To"] = "ops@example.org"
    msg["Subject"] = "Renewal of the Acme contract"
    msg["Date"] = "Wed, 01 Oct 2025 09:00:00 +0000"
    msg.set_content("Hi, please find the renewal invoice attached.")
    msg.add_attachment(b"Invoice 2025-117: 1,500 due 31 October.\n", maintype="text", subtype="plain",
                       filename="invoice.txt")
    files["message.eml"] = bytes(msg)
    return files


def write_snapshots() -> None:
    """The contract snapshots `test_knowledge_contract_frozen` compares against."""
    from core.knowledge.testing.snapshot import api_schema, contract_snapshot

    (HERE / "contract_snapshot.json").write_text(
        json.dumps(contract_snapshot(), indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n", "utf-8")
    (HERE / "openapi_knowledge.json").write_text(
        json.dumps(api_schema(), indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n", "utf-8")
    print("wrote contract_snapshot.json and openapi_knowledge.json")


def main() -> None:
    if "--snapshots" in sys.argv:
        write_snapshots()
        return
    parsed_dir = HERE / "parsed"
    parsed_dir.mkdir(exist_ok=True)
    for name, doc in parsed_fixtures().items():
        (parsed_dir / f"{name}.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", "utf-8")
    corpus_dir = HERE / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    for name, data in corpus_files().items():
        (corpus_dir / name).write_bytes(data)
    print(f"wrote {len(parsed_fixtures())} parsed fixtures and {len(corpus_files())} corpus files")
    write_snapshots()


if __name__ == "__main__":
    main()
