"""`parse_pdf` and `parse_xlsx` must not reach private networks (S6).

Both tools fetch a model-supplied URL. Until this was fixed they did it with a
bare `requests.get`: no egress check, redirects followed, no timeout and no
size cap. Measured on the cloud dev fleet (2026-09-25, D128), from inside the
worker:

  * a PDF served on an internal address came back as text, through a redirect too;
  * every internal service answered, and the error text told refused from
    closed from 403 — a port scan;
  * the metadata address hung the call until it was killed;
  * a 300 MB body was read whole into memory before parsing began.

On the fleet these are shared tool subprocesses, so a hang or a blow-up is
every tenant's problem on that worker, not just the caller's.

The stand-in for "the public internet" is a loopback server named in
`EGRESS_ALLOW_HOSTS`. `localhost` resolves to the same server but is not on the
list, so a hop there is a hop into a blocked range — the redirect case.

Backported to 1.9.2 with two differences from the development line: no
`openWorldHint` (1.9.x declares no tool annotations), and no check that a
spreadsheet parses — in 1.9.x it never has, because `to_markdown` needs
`tabulate`, which nothing installs (F172, fixed on the development line and
deliberately not in a security release). Every refusal is checked for both tools.
"""
from __future__ import annotations

import http.server
import io
import threading
import time

import pytest

from core import egress_guard
from tools import pdf_parser, xlsx_parser

MARKER = "SECRET-MARKER-42"


def _pdf(text: str) -> bytes:
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _xlsx(text: str) -> bytes:
    import pandas as pd

    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as writer:
        pd.DataFrame({"name": ["alpha", "beta"], "value": [text, 2]}).to_excel(
            writer, sheet_name="First", index=False
        )
        pd.DataFrame().to_excel(writer, sheet_name="Blank", index=False)
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, length: bool = True):
        self.send_response(200)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/doc.pdf":
            self._send(_pdf(MARKER))
        elif path == "/sheet.xlsx":
            self._send(_xlsx(MARKER))
        elif path.startswith("/redirect?to="):
            self.send_response(302)
            self.send_header("Location", path.split("?to=", 1)[1])
            self.end_headers()
        elif path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif path == "/hang":
            time.sleep(30)
        elif path == "/big-declared":
            self.send_response(200)
            self.send_header("Content-Length", str(10_000_000))
            self.end_headers()
            try:
                self.wfile.write(b"\0" * 10_000_000)
            except OSError:
                pass
        elif path == "/big-undeclared":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"\0" * 100_000)
            except OSError:
                pass
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture(autouse=True)
def _public_is_127(monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


async def _pdf_text(url: str) -> str:
    return (await pdf_parser.call_tool("parse_pdf", {"file_url": url}))[0].text


async def _xlsx_text(url: str) -> str:
    return (await xlsx_parser.call_tool("parse_xlsx", {"file_url": url}))[0].text


PARSERS = [pytest.param(_pdf_text, "doc.pdf", id="pdf"), pytest.param(_xlsx_text, "sheet.xlsx", id="xlsx")]


# ---------------------------------------------------------------------------
# What must still work
# ---------------------------------------------------------------------------


async def test_a_pdf_from_an_allowed_host_still_parses(server):
    text = await _pdf_text(f"http://127.0.0.1:{server}/doc.pdf")
    assert "--- Page 1 ---" in text
    assert MARKER in text


async def test_a_redirect_between_allowed_hosts_is_followed(server):
    target = f"http://127.0.0.1:{server}/doc.pdf"
    text = await _pdf_text(f"http://127.0.0.1:{server}/redirect?to={target}")
    assert MARKER in text


# ---------------------------------------------------------------------------
# What must be refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("read, name", PARSERS)
@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://localhost:{port}/{name}", "loopback"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://169.254.170.2/v2/credentials", "link-local"),
        ("http://10.0.0.1/{name}", "private network"),
        ("http://[::ffff:169.254.170.2]/", "link-local"),
        ("file:///etc/passwd", "not supported"),
    ],
)
async def test_an_internal_address_is_refused_before_any_connection(server, read, name, url, reason):
    started = time.monotonic()
    text = await read(url.format(port=server, name=name))
    assert reason in text, text
    assert MARKER not in text
    # Refused up front: the metadata address used to hang until killed.
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("read, name", PARSERS)
async def test_a_redirect_into_a_blocked_range_is_refused(server, read, name):
    hop = f"http://localhost:{server}/{name}"
    text = await read(f"http://127.0.0.1:{server}/redirect?to={hop}")
    assert "loopback" in text, text
    assert MARKER not in text


@pytest.mark.parametrize("read, name", PARSERS)
async def test_a_redirect_to_the_metadata_endpoint_is_refused(server, read, name):
    hop = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    text = await read(f"http://127.0.0.1:{server}/redirect?to={hop}")
    assert "link-local" in text, text


@pytest.mark.parametrize("read, name", PARSERS)
async def test_a_redirect_loop_gives_up(server, read, name):
    text = await read(f"http://127.0.0.1:{server}/loop")
    assert "redirect" in text.lower(), text


@pytest.mark.parametrize("read, name", PARSERS)
@pytest.mark.parametrize("path", ["big-declared", "big-undeclared"])
async def test_a_body_over_the_cap_is_refused(server, monkeypatch, read, name, path):
    monkeypatch.setattr("core.safe_fetch.MAX_BYTES", 1024 * 1024)
    text = await read(f"http://127.0.0.1:{server}/{path}")
    assert "1 MB" in text, text


@pytest.mark.parametrize("read, name", PARSERS)
async def test_a_server_that_never_answers_times_out(server, monkeypatch, read, name):
    monkeypatch.setattr("core.safe_fetch.TOTAL_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()
    text = await read(f"http://127.0.0.1:{server}/hang")
    assert "did not finish" in text, text
    assert time.monotonic() - started < 5
