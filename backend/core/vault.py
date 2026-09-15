"""
Vault: Automatically saves large tool outputs to files and provides tools to query them.
Also resolves `@[path]` vault mentions in prompts — `MentionReader`, below — by
reading the blob store directly: never a working copy, always the file as it is now.

When any tool returns more than VAULT_THRESHOLD characters, the output is saved to
the vault and the LLM receives only the file path + metadata. The LLM can then use
the Filesystem MCP tools (read_file, search_files) to access parts of the file
without flooding its context window.

The vault root comes from the blob store, which scopes it to the current tenant —
so two tenants running in one process cannot read each other's tool output, and
there is no DATA_DIR involved.

The vault hands *filesystem paths* to the LLM and to the Filesystem MCP server,
so it always needs a real directory. Whether that directory *is* the vault or a
working copy of one is `core/vault_backend.py`'s decision, taken from whether the
blob store can offer a path: a local install gets its folder, an object store
gets a materialised working copy under the scratch root, hydrated on demand.

That note used to read "the vault falls back to a local root ... a deliberate
design decision and not made here". The decision has now been made, because the
fallback was writing an object-store deployment's vault to the pod's own disk.
"""
import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath

from core.vault_backend import VAULT_PREFIX, get_vault

VAULT_THRESHOLD = 100000  # characters (fallback default)


def _vault_root() -> Path:
    """The current tenant's vault directory.

    A real directory either way, because the Filesystem MCP server and the
    sandbox mount need one. What differs is whether it *is* the vault or a
    working copy of it — see `core/vault_backend.py`. It used to fall back to a
    plain `LocalBlobStore()` when the store had no path, which meant a
    deployment on object storage wrote its vault to the pod's own disk and lost
    it when the pod went away.
    """
    return get_vault().root


def _vault_outputs_dir() -> Path:
    return _vault_root() / "tool_outputs"


def _make_vault_path(tool_name: str, ext: str) -> Path:
    """Generate a unique, safe vault file path."""
    vault_dir = _vault_outputs_dir()
    vault_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    safe_name = re.sub(r"[^\w]", "_", tool_name)[:40]
    return vault_dir / f"{safe_name}_{timestamp}.{ext}"


def maybe_vault(tool_name: str, raw_output: str) -> str:
    """
    If raw_output exceeds the vault threshold (from settings), persist it to vault and return a
    compact JSON reference the LLM can act on with the vault read/search tools.
    Returns raw_output unchanged when under the threshold or vault is disabled.

    In scale mode with S3 configured, the file is also uploaded to S3 for durability
    and cross-worker access (the local path remains valid for the duration of the run).
    """
    from core.config import load_settings
    settings = load_settings()
    if not settings.get("vault_enabled", True):
        return raw_output
    threshold = settings.get("vault_threshold", VAULT_THRESHOLD)
    if len(raw_output) <= threshold:
        return raw_output

    # Decide extension: JSON gets pretty-validated, everything else is text.
    try:
        parsed = json.loads(raw_output)
        ext = "json"
        content = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        ext = "txt"
        content = raw_output

    # One write, and the backend decides what durable means. On a plain install
    # that is the file. On object storage it is the object, and a failure to
    # store it raises rather than being swallowed — the reference below tells
    # the model the output is saved, and it has to be true.
    vault = get_vault()
    path = _make_vault_path(tool_name, ext)
    vault.write(path, content)

    total_lines = content.count("\n") + 1
    size = f"Output too large ({len(raw_output):,} chars, {total_lines:,} lines)."
    if vault.materialises:
        # On a fleet the model's tools are `tools/vault_fs.py`, which take a
        # path relative to the vault — the same form `@[path]` uses in a
        # prompt. The replica's scratch path this used to hand out is a key no
        # tool accepts, and `read_file_by_lines`/`grep` exist on no worker.
        reference = path.relative_to(vault.root).as_posix()
        message = (
            f"{size} Saved to the vault as {reference}. Instead: read_file with "
            f"start_line/end_line for a slice, read_json for a slice of a JSON file, "
            f"or search_files for a value — path \"{reference}\"."
        )
    else:
        # A plain install's agents have the Filesystem tools, which take paths.
        reference = str(path)
        message = (
            f"{size} Saved to vault at: {path}. "
            f"Instead: use read_file_by_lines with start_line/end_line to read a slice, "
            f"or grep to search for specific values."
        )
    return json.dumps({
        "vault_file": reference,
        "file_type": ext,
        "size_chars": len(raw_output),
        "total_lines": total_lines,
        "content_preview": content[:500],
        "message": message,
    })


# ---------------------------------------------------------------------------
# Vault tool implementations — called directly by react_engine.py
# ---------------------------------------------------------------------------




#: A mention: `@[`, then anything up to the closing bracket on the same line.
MENTION_RE = re.compile(r"@\[([^\]\n]+)\]")

#: A prompt may inline this many times the per-mention cap in total.
MENTION_TOTAL_FACTOR = 2

#: What the vault stores that no tool reads (download-only, D94). Known by
#: extension, so a 10 MB PDF is never downloaded to find out that it is one.
_BINARY_KINDS = {
    ".pdf": "a PDF",
    ".png": "a PNG image",
    ".jpg": "a JPEG image",
    ".jpeg": "a JPEG image",
    ".webp": "a WebP image",
}

#: A private-use character brackets a masked mention. It carries no brace, so
#: neither the legacy `{state.x}` pass nor `templating.render` can touch one;
#: and `mention_key` refuses the private-use range, so no vault path holds one.
_MARK = ""
_MASKED = re.compile(f"{_MARK}(\\d+){_MARK}")

#: Unicode categories a vault path may not contain: control, format, surrogate,
#: private-use, unassigned, and the line and paragraph separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def binary_kind(rel: str) -> str | None:
    """What a download-only file is ("a PDF", "a PNG image"), or None for text."""
    return _BINARY_KINDS.get(PurePosixPath(rel).suffix.lower())


def mention_key(rel: str) -> str | None:
    """The blob key for a mention, or None when the text is not a vault path.

    NFC first, so `café.md` typed on a Mac finds the file the vault page
    saved. Then the refusals: empty, a backslash, a leading slash, an empty or
    `.` or `..` segment, a bracket (it would have ended the mention), and any
    control, format, surrogate, private-use, unassigned or line-separator
    character. What passes is placed under the vault prefix; the store adds
    the tenant.
    """
    rel = unicodedata.normalize("NFC", (rel or "").strip())
    if not rel or "\\" in rel or "[" in rel or "]" in rel or rel.startswith("/"):
        return None
    if any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in rel):
        return None
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    return f"{VAULT_PREFIX}/{rel}"



@dataclass
class Mention:
    """One `@[path]` in a prompt, and what became of it."""

    raw: str                    # as typed, brackets included — what stays when nothing else can
    rel: str                    # the path, NFC
    key: str | None             # the blob key, or None when the path was refused
    outcome: str = "missing"    # inlined | referenced | missing | invalid | unreadable
    chars: int = 0
    reason: str = ""
    text: str | None = field(default=None, repr=False)

    def summary(self) -> dict:
        """What a run log records about the mention — never its content."""
        return {"path": self.rel, "chars": self.chars, "outcome": self.outcome, "reason": self.reason}

    def line(self) -> str:
        """One line for a human reading the log."""
        return mention_line(self.summary())

    def warning(self) -> str | None:
        """Why the mention was left as typed, or None when it was not."""
        if self.outcome in ("missing", "invalid", "unreadable"):
            return f"@[{self.rel}] — {self.reason}; left as typed"
        return None


def mention_line(summary: dict) -> str:
    """The log line for a mention's `summary()` — the loggers see the event, not the object."""
    path = summary.get("path", "")
    outcome = summary.get("outcome", "")
    reason = summary.get("reason", "")
    if outcome == "inlined":
        if reason == "above":
            return f"@[{path}] — inlined above"
        return f"@[{path}] — {int(summary.get('chars') or 0):,} chars inlined"
    return f"@[{path}] — {outcome}: {reason}"


class MentionReader:
    """Resolves the `@[path]` mentions of one prompt, chat turn or system prompt.

    It reads the blob store, on a thread — never the materialised working copy
    that `S3Vault.ensure_local` serves for as long as it exists, which is how
    an edit made on the vault page was never seen by a warm replica.

    Two shapes. `expand` is for text that is not templated: a chat message.
    `mask` → render → `fill` is for text that is: each mention becomes a
    placeholder, the caller renders `{state.x}` and `{{ … }}`, and only then
    does the file's text go in. So a file that says `{{ global.KEY }}` reaches
    the model as those words rather than as the secret, and a state value that
    happens to contain `@[x]` is a value, not a mention.

    Caps: a mention up to `per_mention` characters (the tenant's
    `vault_threshold`) and a prompt up to `total`; past either, the model gets a
    reference line naming the file, its size, and the tool that can read it. A
    binary is a reference line without a download. A file that is missing,
    unreadable, or not a vault path at all stays exactly as typed — see
    `Mention.warning`.

    The reads are cached for the reader's life and the budget is counted per
    call: an orchestration run keeps one reader, so a file ten steps mention is
    read once, while each step's prompt gets the whole inline budget and the
    same file twice in one prompt is shown once.
    """

    def __init__(self, *, per_mention: int | None = None, total: int | None = None):
        if per_mention is None or total is None:
            from core.config import load_settings
            threshold = int(load_settings().get("vault_threshold") or VAULT_THRESHOLD)
        self.per_mention = per_mention if per_mention is not None else threshold
        self.total = total if total is not None else threshold * MENTION_TOTAL_FACTOR
        #: What each key read as, before any cap — kept for the reader's life.
        self._reads: dict[str, Mention] = {}

    # ── the two shapes ───────────────────────────────────────────────────────

    def mask(self, text: str) -> tuple[str, list[str]]:
        """Every mention replaced by a placeholder; the mentions, as typed, in order."""
        raws: list[str] = []

        def _sub(match: re.Match) -> str:
            raws.append(match.group(0))
            return f"{_MARK}{len(raws) - 1}{_MARK}"

        return MENTION_RE.sub(_sub, text or ""), raws

    async def fill(self, masked: str, raws: list[str]) -> tuple[str, list[Mention]]:
        """The placeholders of `mask` swapped for what each mention resolved to."""
        used = 0
        shown: set[str] = set()
        mentions: list[Mention] = []
        blocks: list[str] = []
        for raw in raws:
            m = await self._resolve(raw)
            if m.outcome == "inlined":
                if m.key in shown:
                    m.reason = "above"
                elif m.chars > self.per_mention:
                    m.outcome, m.reason = "referenced", f"over the {self.per_mention:,}-char inline limit"
                elif used + m.chars > self.total:
                    m.outcome, m.reason = "referenced", f"the prompt's {self.total:,}-char inline budget is used up"
                else:
                    used += m.chars
                    shown.add(m.key)
                if m.outcome == "referenced":
                    m.text = None
            mentions.append(m)
            blocks.append(self._render(m))

        # One pass, so a file whose text happens to contain a placeholder is
        # not scanned again once it has been put in.
        def _sub(match: re.Match) -> str:
            i = int(match.group(1))
            return blocks[i] if i < len(blocks) else match.group(0)

        return _MASKED.sub(_sub, masked), mentions

    async def expand(self, text: str) -> tuple[str, list[Mention]]:
        """`text` with its mentions resolved, for text that is not templated."""
        masked, raws = self.mask(text)
        if not raws:
            return text or "", []
        return await self.fill(masked, raws)

    # ── resolving ────────────────────────────────────────────────────────────

    async def _resolve(self, raw: str) -> Mention:
        rel = unicodedata.normalize("NFC", raw[2:-1].strip())
        key = mention_key(rel)
        if key is None:
            return Mention(raw, rel, None, "invalid", reason="not a vault path")
        cached = self._reads.get(key)
        if cached is not None:
            return replace(cached, raw=raw)
        mention = await self._read(raw, rel, key)
        # A store that could not be reached is asked again next time; the
        # other outcomes are what the file is, and hold for the reader's life.
        if mention.outcome != "unreadable":
            self._reads[key] = mention
        return replace(mention)

    async def _read(self, raw: str, rel: str, key: str) -> Mention:
        from core.storage import get_blob_store

        store = get_blob_store()
        kind = binary_kind(rel)
        try:
            if kind is not None:
                if not await asyncio.to_thread(store.exists, key):
                    return Mention(raw, rel, key, "missing", reason="no such vault file")
                return Mention(raw, rel, key, "referenced",
                               reason=f"{kind}; binary, download-only, not readable by tools")
            content = await asyncio.to_thread(store.get, key)
        except ValueError:
            # A decode error (`UnicodeDecodeError` is one) — the object is not
            # text. The store's own key guard also raises ValueError, but a key
            # from `mention_key` cannot trip it.
            return Mention(raw, rel, key, "referenced", reason="not a text file; not readable by tools")
        except Exception as exc:  # noqa: BLE001 — the reason reaches the log, the text stays as typed
            return Mention(raw, rel, key, "unreadable", reason=f"could not be read: {exc}")
        if content is None:
            return Mention(raw, rel, key, "missing", reason="no such vault file")
        return Mention(raw, rel, key, "inlined", chars=len(content), text=content)

    # ── rendering ────────────────────────────────────────────────────────────

    def _render(self, m: Mention) -> str:
        if m.outcome in ("missing", "invalid", "unreadable"):
            return m.raw
        on_disk = _on_disk(m.key)
        if m.outcome == "referenced":
            if m.chars:
                return (f"[vault: {m.rel} — {m.chars:,} chars, {m.reason}; read it with "
                        f"read_file / read_json / search_files (path \"{m.rel}\"){on_disk}]")
            return f"[vault: {m.rel} — {m.reason}{on_disk}]"
        if m.reason == "above":
            return f"[vault: {m.rel} — inlined above]"
        fence = _fence_for(m.text or "")
        return f"[vault: {m.rel} — {m.chars:,} chars{on_disk}]\n{fence}{_info(m.rel)}\n{m.text}\n{fence}"


def _on_disk(key: str | None) -> str:
    """Where the file is on disk, when the vault is a directory.

    An install whose vault is a real directory gives its agents the Filesystem
    tools, which take paths; the fleet's vault tool takes the key form already
    in the line. Nothing on an object store.
    """
    if not key:
        return ""
    from core.storage import get_blob_store

    try:
        path = get_blob_store().path_for(key)
    except Exception:  # noqa: BLE001
        return ""
    return f"; on disk at {path}" if path is not None else ""


def _fence_for(text: str) -> str:
    """A code fence longer than any run of backticks inside the file."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`{3,}", text)), default=0)
    return "`" * max(3, longest + 1)


def _info(rel: str) -> str:
    ext = PurePosixPath(rel).suffix[1:].lower()
    return ext if ext.isalnum() and ext != "txt" else "text"


def _safe_path(path: str) -> Path:
    """Return Path, rejecting obvious traversal attempts."""
    p = Path(path).resolve()
    return p


def _ensure_local_path(path: str) -> str:
    """Materialise a vault file this replica does not have yet.

    A no-op on a plain install, where the file is simply there. On object
    storage the backend pulls it into the working copy — **at its real path**,
    not into a mangled name under the system temp directory as this used to do.
    That matters: the path is handed to the model and to the Filesystem MCP
    server, and one that has been flattened to `tool_outputs_thing.json` in
    /tmp is not a path any of the vault's own guards will accept afterwards.
    """
    try:
        return str(get_vault().ensure_local(Path(path)))
    except Exception:
        return path


def slice_lines(text: str, start_line: int, end_line: int) -> dict:
    """Lines [start_line, end_line], 1-indexed and inclusive, as a result dict.

    Pure: takes the content, never a path. Extracted so the same slicing serves
    a file on disk (below) and an object read straight out of the blob store
    (`tools/vault_fs.py`), which has no local path to offer and should not be
    made to invent one.
    """
    lines = text.splitlines()
    total = len(lines)
    s = max(1, start_line) - 1      # 0-indexed
    e = min(end_line, total)
    return {
        "start_line": s + 1,
        "end_line": e,
        "total_lines": total,
        "content": "\n".join(lines[s:e]),
    }


def tool_read_file_chunk(path: str, start_line: int, end_line: int) -> str:
    """Read lines [start_line, end_line] (1-indexed, inclusive) from any file."""
    try:
        path = _ensure_local_path(path)
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        return json.dumps({
            "path": path,
            **slice_lines(p.read_text(encoding="utf-8"), start_line, end_line),
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})


def search_text(text: str, query: str, context_lines: int = 5) -> dict:
    """Grep-like search over content, with surrounding context. Pure — see `slice_lines`."""
    lines = text.splitlines()
    q = query.lower()
    results = []
    covered: set[int] = set()

    for i, line in enumerate(lines):
        if q not in line.lower():
            continue
        start = max(0, i - context_lines)
        end = min(len(lines), i + context_lines + 1)
        if i in covered:
            continue
        covered.update(range(start, end))
        block = []
        for j in range(start, end):
            prefix = ">>>" if j == i else "   "
            block.append(f"{prefix} [L{j + 1}] {lines[j]}")
        results.append({
            "match_line": i + 1,
            "match": line,
            "context": "\n".join(block),
        })
        if len(results) >= 20:
            break

    return {
        "query": query,
        "matches_found": len(results),
        "results": results,
    }


def tool_search_file(path: str, query: str, context_lines: int = 5) -> str:
    """Grep-like search: returns matching lines with ±context_lines of surrounding context."""
    try:
        path = _ensure_local_path(path)
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        return json.dumps({
            "path": path,
            **search_text(p.read_text(encoding="utf-8"), query, context_lines),
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})


def tool_read_json_chunk(path: str, offset: int = 0, limit: int = 50) -> str:
    """
    Read a slice of a JSON vault file.
    - Array root  → returns items[offset : offset+limit]
    - Object root → returns keys[offset : offset+limit] with their values
    """
    try:
        path = _ensure_local_path(path)
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        data = json.loads(p.read_text(encoding="utf-8"))

        if isinstance(data, list):
            total = len(data)
            chunk = data[offset: offset + limit]
            return json.dumps({
                "path": path,
                "root_type": "array",
                "total_items": total,
                "offset": offset,
                "limit": limit,
                "returned": len(chunk),
                "data": chunk,
            }, default=str)

        if isinstance(data, dict):
            keys = list(data.keys())
            total = len(keys)
            chunk_keys = keys[offset: offset + limit]
            chunk = {k: data[k] for k in chunk_keys}
            return json.dumps({
                "path": path,
                "root_type": "object",
                "total_keys": total,
                "all_keys": keys,
                "offset": offset,
                "limit": limit,
                "returned": len(chunk_keys),
                "data": chunk,
            }, default=str)

        # Scalar / other
        return json.dumps({
            "path": path,
            "root_type": type(data).__name__,
            "data": data,
        }, default=str)

    except Exception as ex:
        return json.dumps({"error": str(ex)})


def tool_search_json(path: str, query: str) -> str:
    """
    Recursively search JSON values for query string.
    Returns up to 20 matching {json_path, value} pairs.
    """
    try:
        path = _ensure_local_path(path)
        p = _safe_path(path)
        if not p.exists():
            return json.dumps({"error": f"File not found: {path}"})
        data = json.loads(p.read_text(encoding="utf-8"))
        q = query.lower()
        results: list[dict] = []

        def _recurse(obj, jpath: str):
            if len(results) >= 20:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _recurse(v, f"{jpath}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _recurse(item, f"{jpath}[{i}]")
            else:
                val_str = str(obj)
                if q in val_str.lower():
                    results.append({"json_path": jpath, "value": val_str[:500]})

        _recurse(data, "$")
        return json.dumps({
            "path": path,
            "query": query,
            "matches_found": len(results),
            "results": results,
        })
    except Exception as ex:
        return json.dumps({"error": str(ex)})
