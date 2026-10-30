"""A step's output held as a file in the vault and passed on by reference (D339 P4).

A step passed its output on as text: a crawling agent retyped 39 pages into its answer, and the next step's prompt,
the context of what earlier steps did and the run's answer each pasted that text in full (TRI1's triage, S4). Now an
output over the spill line is saved to the vault, and state holds a reference to it:

    {"$vault": "step_outputs/<run>/<step>/<name>.txt", "chars": 150000, "kind": "text"}

with `"kind": "json"` and an `index` (what the object holds, `vault.outline`) for an output that was an object, and
`"text"` for the note an agent finished with beside its file.

**A reference is the value to code** — a condition, a set argument, a tool's template and a transform read what was
saved, as the type it was (`load`, `resolved`). **Only a model's prompt and a person's text see the file**: a prompt
has it inline when it fits that step's read share and otherwise the file to read (`for_prompt`); a Print, a Human
prompt and the run's answer show an excerpt and the file's key (`for_person`).

What is saved is scrubbed of secrets first, as compaction's archive is (F854); the file is the vault's, so on the
fleet it is the tenant's, through the store (`tools/vault_fs.py`).
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

#: The key a reference is known by.
REF = "$vault"
#: Where step outputs are saved, under the vault.
FOLDER = "step_outputs"
#: What a person is shown of a saved output before its key.
EXCERPT_CHARS = 600
#: The index shown beside a saved object in a prompt, at most.
INDEX_CHARS = 1_200
#: Loaded outputs kept in this process, in characters: a run's steps read the same file more than once.
CACHE_CHARS = 8_000_000

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_cache: "OrderedDict[str, Any]" = OrderedDict()
_cached_chars = 0
#: A host's check before a write (cloud: the workspace's vault quota). Raises to refuse; None in a plain install.
_write_hook: Callable[[int], None] | None = None


#: The read share of the model whose prompt is being built (`context_budget.Budget.read_cap`), set by its step.
_reading: ContextVar[int | None] = ContextVar("step_refs_reading", default=None)


@contextmanager
def reading(cap: int | None) -> Iterator[None]:
    """While a step builds its model's prompt: a saved output up to `cap` characters is inline in it."""
    token = _reading.set(cap)
    try:
        yield
    finally:
        _reading.reset(token)


def read_cap_now() -> int | None:
    return _reading.get()


class MissingOutput(LookupError):
    """A reference whose file is not in the vault."""


def set_write_hook(hook: Callable[[int], None] | None) -> None:
    """Called with each write's size in bytes before it lands; it raises to refuse (cloud's vault quota)."""
    global _write_hook
    _write_hook = hook


def forget_cache() -> None:
    global _cached_chars
    _cache.clear()
    _cached_chars = 0


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get(REF), str)


def _part(text: str) -> str:
    """One path part: no separators, no `..`, never empty."""
    cleaned = _SAFE.sub("_", str(text or "")).strip("._") or "_"
    return cleaned[:80]


def key_for(run_id: str, step_id: str, name: str) -> str:
    return f"{FOLDER}/{_part(run_id)}/{_part(step_id)}/{_part(name)}"


def _text_of(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        return value, "text"
    return json.dumps(value, ensure_ascii=False, default=str), "json"


def _write(key: str, text: str) -> None:
    from core.secret_scrub import scrub_secrets
    from core.vault_backend import get_vault

    clean = scrub_secrets(text)
    if _write_hook is not None:
        _write_hook(len(clean.encode("utf-8")))
    vault = get_vault()
    vault.write(vault.root / key, clean)


def save(run_id: str, step_id: str, value: Any, *, name: str = "", note: str = "") -> dict:
    """Save `value` (text, or an object as JSON) and return its reference.

    Named by what it holds unless `name` is given: a step inside a loop saves one file per output, never one file
    each overwrites, and a resumed step that saves the same output again names the same file."""
    import hashlib

    text, kind = _text_of(value)
    name = name or f"output-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"
    key = key_for(run_id, step_id, f"{name}.{'json' if kind == 'json' else 'txt'}")
    _write(key, text)
    ref: dict[str, Any] = {REF: key, "chars": len(text), "kind": kind}
    if note:
        ref["text"] = note
    if kind == "json":
        from core.vault import outline

        ref["index"] = outline(value)
    _remember(key, value, len(text))
    return ref


def file_ref(key: str, *, note: str = "") -> dict:
    """A reference to a file already in the vault (one an agent wrote or read), or MissingOutput."""
    text = _read(key)
    ref: dict[str, Any] = {REF: key, "chars": len(text), "kind": "text"}
    if note:
        ref["text"] = note
    return ref


def spill(run_id: str, step_id: str, value: Any, *, at: int) -> Any:
    """`value`, or its reference when it is over `at` characters. A reference, None and small values pass through."""
    if value is None or is_ref(value) or at <= 0:
        return value
    text, _kind = _text_of(value)
    if len(text) <= at:
        return value
    return save(run_id, step_id, value)


def _remember(key: str, value: Any, chars: int) -> None:
    global _cached_chars
    if chars > CACHE_CHARS // 4:
        return
    if key in _cache:
        _cache.move_to_end(key)
        return
    _cache[key] = (value, chars)
    _cached_chars += chars
    while _cached_chars > CACHE_CHARS and _cache:
        _old, (_v, c) = _cache.popitem(last=False)
        _cached_chars -= c


def _read(key: str) -> str:
    """The file's text from the store, which is the vault itself (a pod's working copy may not have it)."""
    from core.storage import get_blob_store
    from core.vault_backend import _PREFIX

    text = get_blob_store().get(f"{_PREFIX}/{key}")
    if text is None:
        raise MissingOutput(f"the saved output {key} is not in the vault")
    return text if isinstance(text, str) else text.decode("utf-8")


def load(ref: dict) -> Any:
    """What `ref` saved, as the type it was."""
    key = ref[REF]
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key][0]
    text = _read(key)
    value: Any = text
    if ref.get("kind") == "json":
        try:
            value = json.loads(text)
        except ValueError:
            value = text
    _remember(key, value, len(text))
    return value


def resolved(value: Any) -> Any:
    """`value`, with a reference loaded."""
    return load(value) if is_ref(value) else value


def resolved_state(state: dict) -> dict:
    """A copy of `state` with every top-level reference loaded: what code is given to read."""
    return {k: resolved(v) for k, v in state.items()}


def _note(ref: dict) -> str:
    return f"{ref['text'].strip()}\n" if str(ref.get("text") or "").strip() else ""


def for_prompt(ref: dict, inline_max: int) -> str:
    """A model's view of `ref`: the content when it fits `inline_max`, otherwise the file and how to read it."""
    if int(ref.get("chars") or 0) <= inline_max:
        try:
            content = load(ref)
        except MissingOutput as exc:
            return f"({exc})"
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        return f"{_note(ref)}{text}"
    index = ""
    if ref.get("index") is not None:
        shown = json.dumps(ref["index"], ensure_ascii=False, default=str)
        index = f"; it holds {shown[:INDEX_CHARS]}{'…' if len(shown) > INDEX_CHARS else ''}"
    reader = "read_json (by json_path) or read_file" if ref.get("kind") == "json" else "read_file (by lines)"
    return (f"{_note(ref)}(the file {ref[REF]}: {int(ref.get('chars') or 0):,} characters{index}. It is not pasted "
            f"here, to keep this prompt small: read the parts you need with {reader}, or search it with "
            f"search_files.)")


def for_person(ref: dict) -> str:
    """A person's view of `ref`: an excerpt, then the file's key."""
    try:
        content = load(ref)
    except MissingOutput as exc:
        return f"({exc})"
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    if len(text) <= EXCERPT_CHARS:
        return f"{_note(ref)}{text}"
    head = text[:EXCERPT_CHARS].rstrip()
    return (f"{_note(ref)}{head}… (the whole of it, {len(text):,} characters, is in the vault: "
            f"{ref[REF]})")


# ── an agent step's own files: write one, finish with one (Naveen 10-30: "agents finish with a file") ──────────────

WRITE_OUTPUT = "write_output"
SUBMIT_OUTPUT = "submit_output"
#: One file an agent writes, at most, in bytes; and the files one step may write.
FILE_MAX_BYTES = 2_000_000
FILES_PER_STEP = 10


def output_tools() -> list:
    """The two tools an Agent step without finish tools is offered (D339 P4): they are the engine's own."""
    from core.tools import VirtualTool

    return [
        VirtualTool(
            name=WRITE_OUTPUT,
            description=("Save a file of your work for the steps after this one: a long result, a table, a "
                         "document. Saved to this run's outputs in the Vault; it never overwrites another step's. "
                         "Then finish with submit_output to pass it on, rather than repeating it in your answer."),
            inputSchema={"type": "object", "properties": {
                "name": {"type": "string", "description": "A file name, e.g. pages.md or orders.json"},
                "content": {"type": "string", "description": "The file's whole content"},
            }, "required": ["name", "content"]},
        ),
        VirtualTool(
            name=SUBMIT_OUTPUT,
            description=("Finish this step with a file as its output: one you saved with write_output, or one in "
                         "the Vault you read. `text` is a short note of what it holds, for the next step and the "
                         "person. Use it when your result is long; a short answer is just your reply."),
            inputSchema={"type": "object", "properties": {
                "text": {"type": "string", "description": "One or two sentences: what the file holds"},
                "file": {"type": "string", "description": "The file's key in the Vault, as write_output said it"},
            }, "required": ["text", "file"]},
        ),
    ]


def call_output_tool(name: str, args: dict, *, run_id: str, step_id: str, written: list[str]) -> tuple[str, dict]:
    """Run `write_output` or `submit_output` for one step: (what the model reads, the call's result as an object).
    A result with `error` is a failed call, the model's to fix; `submit_output`'s result is the step's output."""
    args = args if isinstance(args, dict) else {}
    if name == WRITE_OUTPUT:
        file_name, content = str(args.get("name") or "").strip(), args.get("content")
        if not file_name or not isinstance(content, str):
            return "write_output needs a name and the content as text.", {"error": "name and content are needed"}
        if len(written) >= FILES_PER_STEP:
            return (f"This step has saved {FILES_PER_STEP} files, its most. Finish with submit_output.",
                    {"error": "too many files"})
        if len(content.encode("utf-8")) > FILE_MAX_BYTES:
            return (f"That file is over {FILE_MAX_BYTES // 1_000_000} MB, the most one file may be. Save it in parts.",
                    {"error": "too big"})
        key = key_for(run_id, step_id, file_name)
        try:
            _write(key, content)
        except Exception as exc:  # noqa: BLE001 — a quota or a store that refused, said to the model
            return f"The Vault refused the file: {exc}", {"error": str(exc)}
        written.append(key)
        _remember(key, content, len(content))
        return (f"Saved {key} ({len(content):,} characters). Finish with submit_output(text, file=\"{key}\") to "
                f"pass it on.", {"file": key, "chars": len(content)})
    if name == SUBMIT_OUTPUT:
        key, note = str(args.get("file") or "").strip().lstrip("/"), str(args.get("text") or "").strip()
        if not key or ".." in key.split("/"):
            return "submit_output needs the file's key in the Vault.", {"error": "no file"}
        try:
            ref = file_ref(key, note=note)
        except MissingOutput:
            return (f"There is no file {key} in the Vault. Save it with write_output first, or name one you read.",
                    {"error": "no such file"})
        return f"Finished with {key}.", ref
    return f"{name} is not an output tool.", {"error": "unknown"}
