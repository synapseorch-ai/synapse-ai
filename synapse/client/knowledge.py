"""Knowledge from the terminal: bases, uploads, pages, notes and records, documents, search (D294).

All of it is v1's own Knowledge routes (`/api/v1/knowledge/…`, P1a, D176), the
app's handlers behind a key, so the CLI answers as the app and cloud do. While
Knowledge is switched off every one of them is a bare 404, which the CLI says
in words.

An upload is v1's three-step one: open it, PUT each part to the URL the server
hands out with exactly its headers (never the key: the URL is the authority,
cloud's presigned or OSS's own, CCR-8), then complete it with the parts'
ETags. `--wait` reads the document until it is indexed or has failed.
"""
from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path

from synapse.client import errors
from synapse.client.commands import (
    Tree, _cost, _handler, _json_file, _pairs, _pick, _quiet_flag, _when, show_value,
)
from synapse.client.errors import ApiError, CliError
from synapse.client.http import ApiClient
from synapse.client.output import Output

_OFF = ("Knowledge is switched off in this Synapse, so it has no knowledge routes. "
        "Turn it on in Settings → Knowledge.")
#: A document's statuses that end its indexing, for `--wait`.
_SETTLED = frozenset({"indexed", "partial", "failed", "skipped", "deleted"})
_WAIT_SECONDS = 600


def _kb(path: str = "") -> str:
    return "/knowledge/bases" + path


def _call(fn, *args, **kwargs):
    """A Knowledge call, with a bare 404 read as Knowledge being off when it is."""
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        if exc.status == 404 and exc.detail == "Not Found":
            raise CliError(_OFF, errors.NOT_FOUND, 404) from None
        raise


def _base(api: ApiClient, ident: str) -> dict:
    """A knowledge base by id, else by name."""
    try:
        return _call(api.get, _kb(f"/{ident}"))
    except CliError as exc:
        if exc.message == _OFF or exc.status != 404:
            raise
    return _pick(_call(api.get, _kb()), ident, "knowledge base")


def _counts(kb: dict) -> str:
    counts = (kb.get("state") or {}).get("counts_by_status") or {}
    return ", ".join(f"{n} {status}" for status, n in sorted(counts.items()) if n) or "empty"


# ── bases ───────────────────────────────────────────────────────────────────


@_handler
def bases_list(args, api: ApiClient, out: Output):
    bases = _call(api.get, _kb())
    if args.quiet:
        return out.ids(bases)
    if out.json_mode:
        return out.document(bases)
    out.table(bases, [("ID", lambda b: b["id"]), ("NAME", lambda b: b.get("name")),
                      ("FOLDER", lambda b: b.get("folder")), ("DOCUMENTS", _counts),
                      ("THIS MONTH", lambda b: _cost((b.get("state") or {}).get("month_spend_usd"))),
                      ("STATE", lambda b: b.get("status"))],
              empty="No knowledge bases yet. Make one with: synapse knowledge bases create NAME --folder PATH")


def _show_base(out: Output, kb: dict) -> None:
    state = kb.get("state") or {}
    embedding = kb.get("embedding") or {}
    out.fields([("Name", kb.get("name")), ("ID", kb.get("id")), ("Folder", kb.get("folder")),
                ("Description", kb.get("description")), ("State", kb.get("status")),
                ("Documents", _counts(kb)), ("Embedding", embedding.get("model")),
                ("This month", _cost(state.get("month_spend_usd"))),
                ("Budget", _cost(kb.get("budget_monthly_usd"))), ("Paused", state.get("paused_reason")),
                ("Revision", kb.get("revision"))])
    if kb.get("reindex_started"):
        out.note("Rebuilding its index beside the serving one; searches use the old one until it is ready.")


@_handler
def bases_show(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    if out.json_mode:
        return out.document(kb)
    _show_base(out, kb)


def _settings(args) -> dict:
    body = _json_file(args.file, "-f") if getattr(args, "file", None) else {}
    if not isinstance(body, dict):
        raise CliError("-f holds one JSON object: the base's settings.", errors.USAGE)
    for key in ("description", "folder", "language"):
        if getattr(args, key, None) is not None:
            body[key] = getattr(args, key)
    if getattr(args, "budget", None) is not None:
        body["budget_monthly_usd"] = args.budget
    body.update(_pairs(getattr(args, "set", None), "--set"))
    return body


@_handler
def bases_create(args, api: ApiClient, out: Output):
    body = {"name": args.name, **_settings(args)}
    if not body.get("folder"):
        raise CliError("A knowledge base is a Vault folder made searchable: say which with --folder.",
                       errors.USAGE)
    kb = _call(api.post, _kb(), body)
    if out.json_mode:
        return out.document(kb)
    out.text(kb["id"])
    out.note(f"Made {kb.get('name')} over {kb.get('folder')}. Add to it with: "
             f"synapse knowledge upload FILE --base {kb['id']}")


@_handler
def bases_update(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    body = _settings(args)
    if args.name:
        body["name"] = args.name
    if args.disable or args.enable:
        body["status"] = "disabled" if args.disable else "active"
    if not body:
        raise CliError("Say what to change: --name, --description, --folder, --budget, --set or -f.",
                       errors.USAGE)
    # From the revision just read: someone else's edit since is a 409, not overwritten.
    changed = _call(api.patch, _kb(f"/{kb['id']}"), {"revision": kb.get("revision") or 1, **body})
    if out.json_mode:
        return out.document(changed)
    _show_base(out, changed)


@_handler
def bases_delete(args, api: ApiClient, out: Output):
    from synapse.client.commands import _interactive, _yes_no

    kb = _base(api, args.base)
    if not args.yes:
        if not _interactive():
            raise CliError("Deleting a knowledge base asks first. Pass --yes to skip the question.", errors.USAGE)
        if not _yes_no(f"Delete the knowledge base '{kb.get('name')}'? Its index goes; its Vault files stay"):
            out.note("Kept.")
            return errors.OK
    result = _call(api.delete, _kb(f"/{kb['id']}"))
    if out.json_mode:
        return out.document(result)
    out.text("Deleting. Its Vault folder and files stay.")


@_handler
def bases_cost_preview(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    overrides = _json_file(args.file, "-f") if args.file else None
    preview = _call(api.post, _kb(f"/{kb['id']}/cost-preview"), {"overrides": overrides} if overrides else {})
    show_value(out, preview)


@_handler
def bases_costs(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    rows = _call(api.get, _kb(f"/{kb['id']}/costs"), group_by=args.by, since=args.since, until=args.until)
    if out.json_mode:
        return out.document(rows)
    out.table(rows, [(args.by.upper(), lambda r: r.get("key") or r.get(args.by) or r.get("group")),
                     ("USD", lambda r: _cost(r.get("usd") if r.get("usd") is not None else r.get("est_usd"))),
                     ("TOKENS", lambda r: r.get("tokens")), ("CALLS", lambda r: r.get("calls") or r.get("count"))],
              empty="Nothing spent in that window.")


# ── adding to a base ────────────────────────────────────────────────────────


def _wait_for(api: ApiClient, out: Output, doc_ids: list[str]) -> list[dict]:
    """Read each document until its indexing has ended, saying how it is going."""
    deadline = time.monotonic() + _WAIT_SECONDS
    done: dict[str, dict] = {}
    delay = 0.5
    while len(done) < len(doc_ids):
        for doc_id in doc_ids:
            if doc_id in done:
                continue
            doc = _call(api.get, f"/knowledge/documents/{doc_id}")
            if doc.get("status") in _SETTLED:
                done[doc_id] = doc
                style = "green" if doc["status"] == "indexed" else "yellow" if doc["status"] == "partial" else "red"
                why = doc.get("error") or doc.get("status_reason")
                out.note(f"  {doc.get('title') or doc.get('path')}: {doc['status']}" + (f" ({why})" if why else ""),
                         style=style)
        if len(done) == len(doc_ids):
            break
        if time.monotonic() > deadline:
            raise CliError(f"Still indexing after {_WAIT_SECONDS // 60} minutes. Read on with: "
                           f"synapse knowledge docs show {next(d for d in doc_ids if d not in done)}",
                           errors.UNAVAILABLE)
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    return [done[d] for d in doc_ids]


def _accepted(api: ApiClient, out: Output, accepted: dict, wait: bool) -> int:
    docs = (accepted or {}).get("documents") or []
    if wait and docs:
        docs = _wait_for(api, out, [d["id"] for d in docs])
    if out.json_mode:
        out.document({**(accepted or {}), "documents": docs})
    else:
        for doc in docs:
            out.text(doc["id"])
        if not wait and docs:
            out.note(f"Queued. See how it goes with: synapse knowledge docs show {docs[0]['id']}")
    failed = [d for d in docs if d.get("status") == "failed"]
    return errors.ERROR if wait and failed else errors.OK


def _upload_one(api: ApiClient, out: Output, kb_id: str, path: Path, folder: str | None) -> dict:
    size = path.stat().st_size
    if size == 0:
        raise CliError(f"{path} is empty; there is nothing to index.", errors.USAGE)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    session = _call(api.post, _kb(f"/{kb_id}/uploads"),
                    {"filename": path.name, "size_bytes": size, "mime": mime,
                     **({"path": folder} if folder else {})})
    parts = []
    with path.open("rb") as f:
        for part in session["parts"]:
            chunk = f.read(session["part_size"])
            resp = api.put_part(part["url"], chunk, part.get("headers") or {})
            parts.append({"part_number": part["part_number"], "etag": resp.headers.get("etag", "")})
    try:
        return _call(api.post, f"/knowledge/uploads/{session['upload_id']}/complete", {"parts": parts})
    except CliError:
        # Leave nothing half-made behind.
        try:
            api.delete(f"/knowledge/uploads/{session['upload_id']}")
        except CliError:
            pass
        raise


@_handler
def upload(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    paths = [Path(p) for p in args.files]
    for p in paths:
        if not p.is_file():
            raise CliError(f"{p} is not a file.", errors.USAGE)
    documents = []
    for p in paths:
        if not out.json_mode:
            out.note(f"Uploading {p.name} ({p.stat().st_size:,} bytes)…")
        documents += (_upload_one(api, out, kb["id"], p, args.path) or {}).get("documents") or []
    return _accepted(api, out, {"documents": documents}, args.wait)


@_handler
def add_url(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    body = {"urls": args.urls, "kind": args.kind}
    if args.max_depth is not None:
        body["max_depth"] = args.max_depth
    if args.max_pages is not None:
        body["max_pages"] = args.max_pages
    accepted = _call(api.post, _kb(f"/{kb['id']}/urls"), body)
    if out.json_mode or "documents" not in (accepted or {}):
        if not out.json_mode and accepted.get("crawl_id"):
            out.text(accepted["crawl_id"])
            out.note(f"Crawling: {accepted.get('accepted')} start pages queued. Its pages appear in: "
                     f"synapse knowledge docs list --base {kb['id']}")
            return errors.OK
        return out.document(accepted)
    return _accepted(api, out, accepted, args.wait)


@_handler
def add_note(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    text = args.text
    if args.text_file:
        text = os.sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8")
    if not (text or "").strip():
        raise CliError("A note needs its words: TEXT, or --text-file FILE.", errors.USAGE)
    record = {"text": text, **({"title": args.title} if args.title else {}),
              **({"id": args.id} if args.id else {}), "metadata": _pairs(args.meta, "--meta")}
    accepted = _call(api.post, _kb(f"/{kb['id']}/records"), {"records": [record]})
    return _accepted(api, out, accepted, args.wait)


@_handler
def records_add(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    records = _json_file(args.file, "-f")
    if isinstance(records, dict):
        records = records.get("records", [records])
    if not isinstance(records, list) or not records:
        raise CliError("-f holds a JSON list of records: {id?, text or json, title?, metadata?}.", errors.USAGE)
    accepted = _call(api.post, _kb(f"/{kb['id']}/records"), {"records": records})
    return _accepted(api, out, accepted, args.wait)


@_handler
def records_delete(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    result = _call(api.delete, _kb(f"/{kb['id']}/records/{args.record_id}"))
    if out.json_mode:
        return out.document(result)
    out.text("Deleting.")


# ── documents ───────────────────────────────────────────────────────────────


@_handler
def docs_list(args, api: ApiClient, out: Output):
    kb = _base(api, args.base)
    docs, after = [], None
    while True:
        page = _call(api.get, _kb(f"/{kb['id']}/documents"), after=after, limit=min(args.limit or 1000, 1000),
                     status=args.status, path_prefix=args.path_prefix)
        docs += page
        if not page or len(page) < 1000 or (args.limit and len(docs) >= args.limit):
            break
        after = page[-1]["id"]
    docs = docs[: args.limit] if args.limit else docs
    if args.quiet:
        return out.ids(docs)
    if out.json_mode:
        return out.document(docs)
    out.table(docs, [("ID", lambda d: d["id"]), ("PATH", lambda d: d.get("path")),
                     ("STATUS", lambda d: d.get("status")), ("CHUNKS", lambda d: d.get("chunks")),
                     ("UPDATED", lambda d: _when(d.get("indexed_at") or d.get("updated_at")))],
              empty="No documents yet.")


@_handler
def docs_show(args, api: ApiClient, out: Output):
    doc = _call(api.get, f"/knowledge/documents/{args.doc_id}")
    if out.json_mode:
        return out.document(doc)
    out.fields([("Title", doc.get("title")), ("ID", doc.get("id")), ("Path", doc.get("path")),
                ("From", doc.get("source_url") or doc.get("source")), ("Status", doc.get("status")),
                ("Why", doc.get("error") or doc.get("status_reason")), ("Pages", doc.get("pages")),
                ("Chunks", doc.get("chunks")), ("Tokens", doc.get("tokens")),
                ("Indexed", _when(doc.get("indexed_at")))])
    return errors.ERROR if doc.get("status") == "failed" else errors.OK


@_handler
def docs_retry(args, api: ApiClient, out: Output):
    doc = _call(api.post, f"/knowledge/documents/{args.doc_id}/retry")
    return _accepted(api, out, {"documents": [doc]}, args.wait)


@_handler
def docs_delete(args, api: ApiClient, out: Output):
    from synapse.client.commands import _interactive, _yes_no

    if not args.yes:
        if not _interactive():
            raise CliError("Deleting a document asks first. Pass --yes to skip the question.", errors.USAGE)
        doc = _call(api.get, f"/knowledge/documents/{args.doc_id}")
        if not _yes_no(f"Delete '{doc.get('title') or doc.get('path')}' and its Vault file"):
            out.note("Kept.")
            return errors.OK
    result = _call(api.delete, f"/knowledge/documents/{args.doc_id}")
    if out.json_mode:
        return out.document(result)
    out.text("Deleting.")


# ── search ──────────────────────────────────────────────────────────────────


@_handler
def search(args, api: ApiClient, out: Output):
    kb_ids = [_base(api, b)["id"] for b in args.base] if args.base else [b["id"] for b in _call(api.get, _kb())]
    if not kb_ids:
        raise CliError("There are no knowledge bases to search yet.", errors.NOT_FOUND)
    body = {"query": args.query, "kb_ids": kb_ids, "mode": args.mode, "neighbors": args.neighbors,
            "include_trace": args.trace}
    for key, value in (("top_k", args.top_k), ("path_prefix", args.path_prefix), ("rerank", args.rerank),
                       ("score_threshold", args.min_score)):
        if value is not None:
            body[key] = value
    if args.filter:
        body["filters"] = _json_file(args.filter, "--filter") if Path(args.filter).is_file() else _filter(args.filter)
    result = _call(api.post, "/knowledge/search", body)
    if out.json_mode:
        return out.document(result)
    hits = result.get("hits") or []
    if not hits:
        out.note("Nothing matched.")
    for hit in hits:
        cite = hit.get("citation") or {}
        where = " · ".join(dict.fromkeys(x for x in (cite.get("title"), cite.get("path") or hit.get("doc_id")) if x))
        pages = f" p.{cite['page_start']}" if cite.get("page_start") else ""
        out.out.print(f"[bold]{hit.get('label')}[/bold]  {where}{pages}  [dim]{float(hit.get('score') or 0):.3f}[/dim]",
                      markup=True, highlight=False)
        content = " ".join(str(hit.get("content") or "").split())
        out.text(f"    {content[:300]}" + ("…" if len(content) > 300 else ""))
    for what in result.get("degraded") or []:
        out.note(f"! {what}", style="yellow")
    cost = (result.get("cost") or {}).get("total_est_usd")
    out.note(f"query {result.get('query_id')}" + (f" · {_cost(cost)}" if cost else "")
             + f" · its trace: synapse knowledge trace {result.get('query_id')}")


def _filter(text: str) -> dict:
    import json
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise CliError(f"--filter is not JSON: {exc}", errors.USAGE)
    if not isinstance(value, dict):
        raise CliError("--filter is a JSON object, such as '{\"doc_type\": \"contract\"}'.", errors.USAGE)
    return value


@_handler
def trace(args, api: ApiClient, out: Output):
    show_value(out, _call(api.get, f"/knowledge/queries/{args.query_id}"))


# ── the tree ────────────────────────────────────────────────────────────────


def _base_flag(p, required: bool = True) -> None:
    p.add_argument("--base", "--kb", dest="base", required=required, help="the knowledge base: id or name")


def _wait_flag(p) -> None:
    p.add_argument("--wait", action="store_true", help="wait until it is indexed (or has failed)")


def register(tree: Tree) -> None:
    add, group = tree.command, tree.group
    kn = group(tree.sub, "knowledge", "Knowledge bases: add documents, pages and notes, and search them",
               aliases=["kb"])

    bases = group(kn, "bases", "List, make, change and delete knowledge bases")
    _quiet_flag(add(bases, "list", bases_list, "List knowledge bases"))
    add(bases, "show", bases_show, "Show a knowledge base and its state").add_argument("base", help="id or name")
    p = add(bases, "create", bases_create, "Make a knowledge base over a Vault folder")
    p.add_argument("name")
    p.add_argument("--folder", required=True, help="the Vault folder it makes searchable")
    p.add_argument("--description")
    p.add_argument("--language", help="its text-search language (default: simple)")
    p.add_argument("--budget", type=float, metavar="USD", help="the most it may spend a month")
    p.add_argument("-f", "--file", metavar="FILE", help="its settings as JSON (chunking, embedding, rerank, …)")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="one setting; VALUE read as JSON")
    p = add(bases, "update", bases_update, "Change a knowledge base's settings")
    p.add_argument("base", help="id or name")
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--folder")
    p.add_argument("--language")
    p.add_argument("--budget", type=float, metavar="USD")
    on_off = p.add_mutually_exclusive_group()
    on_off.add_argument("--disable", action="store_true", help="stop searching and indexing it")
    on_off.add_argument("--enable", action="store_true")
    p.add_argument("-f", "--file", metavar="FILE", help="settings to change, as JSON")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE")
    p = add(bases, "delete", bases_delete, "Delete a knowledge base (its Vault files stay)")
    p.add_argument("base", help="id or name")
    p.add_argument("--yes", action="store_true", help="do not ask first")
    p = add(bases, "cost-preview", bases_cost_preview, "What indexing a base costs, by stage; starts nothing")
    p.add_argument("base", help="id or name")
    p.add_argument("-f", "--file", metavar="FILE", help="settings to price instead of its own, as JSON")
    p = add(bases, "costs", bases_costs, "What a base has spent")
    p.add_argument("base", help="id or name")
    p.add_argument("--by", choices=["day", "stage", "document", "model"], default="day")
    p.add_argument("--since", help="an ISO date or time (default: this month)")
    p.add_argument("--until")

    p = add(kn, "upload", upload, "Upload files into a knowledge base")
    p.add_argument("files", nargs="+", metavar="FILE")
    _base_flag(p)
    p.add_argument("--path", help="the folder inside the base's to put them in (default: its root)")
    _wait_flag(p)
    p = add(kn, "add-url", add_url, "Add web pages: a page, a sitemap or a whole site")
    p.add_argument("urls", nargs="+", metavar="URL")
    _base_flag(p)
    p.add_argument("--kind", choices=["page", "sitemap", "site"], default="page")
    p.add_argument("--max-depth", type=int, help="with --kind site: how many links deep")
    p.add_argument("--max-pages", type=int)
    _wait_flag(p)
    p = add(kn, "add-note", add_note, "Add a note: a piece of text, kept and searchable")
    p.add_argument("text", nargs="?")
    _base_flag(p)
    p.add_argument("--title")
    p.add_argument("--text-file", metavar="FILE", help="the note's text, from a file; - reads stdin")
    p.add_argument("--id", help="your own id for it: the same id again replaces it")
    p.add_argument("--meta", action="append", metavar="KEY=VALUE", help="metadata to filter searches by")
    _wait_flag(p)
    records = group(kn, "records", "Add or delete records: text or JSON with your own ids and metadata")
    p = add(records, "add", records_add, "Add records from a JSON file (the same id replaces)")
    _base_flag(p)
    p.add_argument("-f", "--file", metavar="FILE", required=True, help="a JSON list of records; - reads stdin")
    _wait_flag(p)
    p = add(records, "delete", records_delete, "Delete a record by its id")
    p.add_argument("record_id")
    _base_flag(p)

    docs = group(kn, "docs", "List, read, retry and delete a base's documents")
    p = add(docs, "list", docs_list, "A base's documents")
    _base_flag(p)
    p.add_argument("--status", help="only these: queued, indexing, indexed, partial, failed, …")
    p.add_argument("--path-prefix", help="only those under this path")
    p.add_argument("--limit", type=int, default=0, help="at most this many (default: all)")
    _quiet_flag(p)
    add(docs, "show", docs_show, "A document and how its indexing went").add_argument("doc_id")
    p = add(docs, "retry", docs_retry, "Index a document again")
    p.add_argument("doc_id")
    _wait_flag(p)
    p = add(docs, "delete", docs_delete, "Delete a document and its Vault file")
    p.add_argument("doc_id")
    p.add_argument("--yes", action="store_true", help="do not ask first")

    p = add(kn, "search", search, "Search knowledge bases")
    p.add_argument("query")
    p.add_argument("--base", "--kb", dest="base", action="append", help="a base to search (default: all); repeat")
    p.add_argument("--top-k", type=int, help="how many results (1–50)")
    p.add_argument("--mode", choices=["hybrid", "lexical", "vector"], default="hybrid")
    p.add_argument("--path-prefix")
    p.add_argument("--filter", metavar="JSON|FILE", help="a metadata filter, such as '{\"year\": 2025}'")
    p.add_argument("--neighbors", type=int, default=0, help="chunks either side of each hit to include (0–3)")
    p.add_argument("--rerank", action=__import__("argparse").BooleanOptionalAction, default=None)
    p.add_argument("--min-score", type=float)
    p.add_argument("--trace", action="store_true", help="include how the search ran")
    add(kn, "trace", trace, "How a search ran: its stages, timings and costs").add_argument("query_id")
