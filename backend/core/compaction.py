"""
Auto context compaction: when accumulated context exceeds a configurable threshold,
ask the LLM to summarise everything to ~30% of its original size, archive the
full original to the vault, and return the compact summary so the agent can continue.

Two-stage strategy (mirrors Claude Code's approach):
  Stage 1 — cheap trim: drop the oldest portion of current_context_text to fit
             within the threshold. No LLM call; no archive.
  Stage 2 — LLM summarise: if Stage 1 alone is not enough (e.g. history is large),
             call the LLM to write a dense summary and archive the full original.

Before either, Stage 0 (D339 P3) moves each older tool observation of any size
into the vault and leaves its key in its place: no model call, and nothing is
lost — the agent reads it back with `read_file`. Prod's agent, its pages
summarised away by Stage 2, fetched the same page four times (TRI1, 10-29).
Stages 1 and 2 run only when Stage 0 was not enough, and Stage 2's archive goes
through the vault's own write, under a key its read tools take (F1248): it was
written to the pod's disk and named by a scratch path no tool accepts.

Both stages keep the request being worked on verbatim at the head (F244). A
mid-turn Stage 2 once summarised 92,899 characters to 2,478 (−97%) and replaced
the transcript wholesale; the question went with it, and ten seconds later the
agent was implementing a feature nobody had asked for. Stage 1 lost it too: it
keeps the newest end, and the request is the transcript's first line. And a
summary that cut below `MIN_KEEP_RATIO` of what it replaced keeps the newest
transcript verbatim beside it, up to that floor.
"""
import math
import re
import secrets
from datetime import datetime
from pathlib import Path

from core.usage_tracker import log_compaction_event

from core.log import get_logger
from core.telemetry import spans as _spans

_log = get_logger(__name__)


def _archive_dir() -> Path:
    """Compaction archives live inside the current tenant's vault.

    Resolved per call rather than bound at import: the vault is tenant-scoped,
    so a module-level constant would pin every tenant in the process to
    whichever one happened to import this module first.
    """
    from core.vault import _vault_root
    return _vault_root() / "compaction_archives"

_COMPACTION_SYS_PROMPT = (
    "You are a context compaction assistant. Your only job is to compress conversation "
    "context into a dense, structured summary that is approximately 30% of the original "
    "length. Preserve ALL of the following exactly:\n"
    "- Key facts, findings, and data values (numbers, names, dates, results)\n"
    "- File paths, URLs, identifiers, and code snippets\n"
    "- Decisions made and the reasoning behind them\n"
    "- Current state: what has been completed and what still needs to be done\n"
    "- Any errors encountered and how they were resolved\n\n"
    "Write as a structured summary with clear sections. Be dense — prefer bullet points "
    "and key-value pairs over prose. Never include filler phrases like 'The assistant then…'\n\n"
    # F244: a summary that lost the question was followed by invented work.
    "The request being worked on is kept verbatim outside your summary. Summarise what has "
    "been done toward it and what is still open — never add a task, goal or plan that the "
    "context does not already contain."
)

#: The request kept at the head of a compacted transcript, at most (F244).
PINNED_REQUEST_MAX_CHARS = 20_000
#: A Stage-2 result never keeps less than this share of what it replaced — the
#: newest transcript, verbatim, fills the difference, then the newest whole
#: messages of the conversation (so it can fall short by one message) — up to
#: half the threshold, so a compacted context is never over it again at once.
MIN_KEEP_RATIO = 0.20


def _pin(request: str | None) -> str:
    """The request line a transcript starts with, clipped head and tail."""
    if request is None:
        return ""
    request = str(request)
    if len(request) > PINNED_REQUEST_MAX_CHARS:
        half = PINNED_REQUEST_MAX_CHARS // 2
        request = (request[:half] + f"\n[… {len(request) - 2 * half:,} characters of the request "
                   "not shown here …]\n" + request[-half:])
    return f"User Request: {request}\n"


def _without_pin(context: str, request: str | None) -> str:
    """The transcript after its request line, when it starts with one."""
    if request is not None:
        head = f"User Request: {request}\n"
        if context.startswith(head):
            return context[len(head):]
    return context


#: An older observation at least this long is moved to the vault by Stage 0; a shorter one stays as it is.
STUB_MIN_CHARS = 2_000
#: The newest tool results Stage 0 leaves verbatim: the working set the next turns act on (Naveen, 10-30, after how
#: Claude Code and Codex keep context: recent results whole, older ones by reference).
KEEP_RESULTS = 3
#: Below the threshold, Stage 0 moves results only when that frees at least this many characters: each pass rewrites
#: the transcript from its first moved result on, and the prompt cache with it, so it runs seldom and in batches.
TIDY_BATCH = 20_000
#: How much of a moved observation its stub still shows, so the transcript says what it was.
STUB_HEAD_CHARS = 240
#: Where an observation starts, and where the transcript's next entry starts after it.
_OBSERVATION = re.compile(r"\n\[Turn (\d+) — Observation\] ?")
_NEXT_ENTRY = re.compile(r"\n(?:\[Turn \d+ — |System: )")


def _vault_key(path: Path) -> str:
    """`path` as the vault's read tools take it: relative to the vault (`tools/vault_fs.py`, `@[path]`)."""
    from core.vault_backend import get_vault

    return path.relative_to(get_vault().root).as_posix()


def _move_observations(body: str, session_id: str, keep_results: int = KEEP_RESULTS,
                       *, at_least: int = 0) -> tuple[str, int, int]:
    """Stage 0: `body` with each observation of at least `STUB_MIN_CHARS`, but its newest `keep_results`, saved to
    the vault and replaced by a line naming its key; with how many were moved and the characters that freed. When
    they would free less than `at_least`, nothing is moved (a tidy pass's batch, `TIDY_BATCH`).

    The saved text is scrubbed of secrets as the archive is (F854). A write that fails leaves that observation
    where it was: the stub says the content is in the vault, and it has to be true."""
    from core.secret_scrub import scrub_secrets
    from core.vault_backend import get_vault

    every = []
    for m in _OBSERVATION.finditer(body):
        nxt = _NEXT_ENTRY.search(body, m.end())
        every.append((m.start(), m.end(), nxt.start() if nxt else len(body), m.group(1)))
    older = every[:-keep_results] if keep_results > 0 else every
    spans = [s for s in older if s[2] - s[1] >= STUB_MIN_CHARS]
    if not spans or sum(end - head_end for _, head_end, end, _ in spans) - 400 * len(spans) < at_least:
        return body, 0, 0
    folder = _make_archive_path(session_id).with_suffix("")
    out, at, moved = [], 0, 0
    for start, head_end, end, turn in spans:
        text = body[head_end:end]
        path = folder / f"turn-{turn}-{start}.txt"
        try:
            get_vault().write(path, scrub_secrets(text))
        except Exception as exc:  # noqa: BLE001 — kept in place rather than lost
            _log.warning(f"⚠️  Stage 0 could not save turn {turn}'s observation: {exc}")
            continue
        head = " ".join(text[:STUB_HEAD_CHARS].split())
        out.append(body[at:head_end])
        out.append(f"(moved to the vault to keep this context small — nothing was cut: {_vault_key(path)}, "
                   f"{len(text):,} chars; read it with read_file) {head}…")
        at, moved = end, moved + 1
    out.append(body[at:])
    moved_body = "".join(out)
    return moved_body, moved, len(body) - len(moved_body)


def _make_archive_path(session_id: str) -> Path:
    archive_dir = _archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    safe_id = re.sub(r"[^\w]", "_", session_id or "session")[:40]
    # A workflow's steps share one session id (`orch_<run>`), and a Parallel's
    # agents now compact side by side (F255): the suffix keeps two archives made
    # in the same 100 µs apart.
    return archive_dir / f"{safe_id}_{timestamp}_{secrets.token_hex(3)}.txt"


def _build_context_block(current_context_text: str, recent_history_messages: list) -> str:
    parts = []
    if recent_history_messages:
        parts.append("=== CONVERSATION HISTORY ===")
        for msg in recent_history_messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                # Anthropic-style content blocks
                content = " ".join(
                    (b.get("text", "") if isinstance(b, dict) else str(b))
                    for b in content
                )
            parts.append(f"[{role}]: {content}")
    if current_context_text.strip():
        parts.append("=== ACCUMULATED TOOL CONTEXT ===")
        parts.append(current_context_text)
    return "\n\n".join(parts)


MIN_STAGE1_SAVINGS_PCT = 20  # Stage-1 trim must save at least this % or we fall through to Stage-2


@_spans.traced_coroutine("synapse.compaction", _spans.compaction_attributes)
async def maybe_compact(
    current_context_text: str,
    recent_history_messages: list,
    settings: dict,
    current_model: str,
    mode: str,
    current_settings: dict,
    session_id: str,
    agent_id: str,
    run_id: str | None = None,
    pinned_request: str | None = None,
) -> tuple[str, list, str | None, dict | None]:
    """
    Returns (context_text, history_messages, archive_path | None, compact_stats | None).

    `pinned_request` is the request the transcript is working on; when given,
    the result starts with it verbatim, whichever stage ran (F244).

    archive_path is only set when Stage-2 LLM summarisation fires (and saves an archive).
    compact_stats is {"stage", "chars_before", "chars_after", "reduction_pct"} when
    compaction fired, or None when it didn't.
    """
    # On by default: with it off, the only thing standing between a long run
    # and a truncated context was the 400k backstop, which is a blunt cut.
    if not settings.get("auto_compact_enabled", True):
        return current_context_text, recent_history_messages, None, None

    threshold = settings.get("auto_compact_threshold", 100000)
    # The tidy line (D339): above it, older tool results go to the vault in batches; the threshold is where
    # anything is summarised. Without one, Stage 0 waits for the threshold as the rest does.
    tidy_at = min(threshold, settings.get("auto_compact_tidy_at") or threshold)
    history_chars = sum(
        len(str(m.get("content", ""))) for m in (recent_history_messages or [])
    )
    total_chars = len(current_context_text) + history_chars

    if total_chars <= tidy_at:
        return current_context_text, recent_history_messages, None, None
    if total_chars <= threshold:
        # Under the threshold: a tidy pass only, worth its rewrite of the cached prefix or not at all.
        pin = _pin(pinned_request)
        moved_body, moved, freed = _move_observations(_without_pin(current_context_text, pinned_request),
                                                      session_id, at_least=TIDY_BATCH)
        if not moved:
            return current_context_text, recent_history_messages, None, None
        tidied = pin + moved_body
        after = len(tidied) + history_chars
        _log_compaction("Stage-0 tidy", total_chars, after)
        await log_compaction_event(stage="refs", chars_before=total_chars, chars_after=after,
                                   session_id=session_id, agent_id=agent_id, run_id=run_id)
        return tidied, recent_history_messages, None, {
            "stage": "refs", "chars_before": total_chars, "chars_after": after,
            "reduction_pct": round(freed / total_chars * 100) if total_chars else 0, "moved": moved,
        }

    _log.debug(
        f"\n📏 Compaction check — "
        f"context {total_chars:,} chars (~{total_chars // 4:,} tokens est.) "
        f"exceeds threshold {threshold:,} chars (~{threshold // 4:,} tokens est.). "
        f"history={history_chars:,} tool_ctx={len(current_context_text):,}. "
        f"Attempting Stage-1 trim…",
    )

    pin = _pin(pinned_request)
    body = _without_pin(current_context_text, pinned_request)

    # ── Stage 0: older observations to the vault, their keys in their place (D339 P3) ──
    # No model call and nothing lost. When that alone brings the context under the
    # threshold, it is the whole compaction; otherwise Stages 1 and 2 work on what is left.
    moved_body, moved, _freed = _move_observations(body, session_id)
    if moved:
        body = moved_body
        current_context_text = pin + body
        stage0_total = len(current_context_text) + history_chars
        if stage0_total <= threshold:
            _log_compaction("Stage-0 refs", total_chars, stage0_total)
            await log_compaction_event(
                stage="refs",
                chars_before=total_chars,
                chars_after=stage0_total,
                session_id=session_id,
                agent_id=agent_id,
                run_id=run_id,
            )
            return current_context_text, recent_history_messages, None, {
                "stage": "refs",
                "chars_before": total_chars,
                "chars_after": stage0_total,
                "reduction_pct": round((total_chars - stage0_total) / total_chars * 100) if total_chars else 0,
                "moved": moved,
            }

    # ── Stage 1: drop oldest tool outputs (cheap — no LLM call, no archive) ──────
    # Keep the most recent portion of current_context_text that fits in the budget.
    # Only accept the Stage-1 result when it saves at least MIN_STAGE1_SAVINGS_PCT;
    # otherwise fall through to Stage-2 LLM summarisation for proper ~30% reduction.
    budget = max(0, threshold - history_chars - len(pin) - 60)  # 60 chars for the trim prefix
    if budget > 0 and len(body) > budget:
        stage1_ctx = pin + "[...older tool outputs trimmed...]\n" + body[-budget:]
        stage1_total = len(stage1_ctx) + history_chars
        stage1_savings_pct = round((total_chars - stage1_total) / total_chars * 100) if total_chars > 0 else 0
        if stage1_total <= threshold and stage1_savings_pct >= MIN_STAGE1_SAVINGS_PCT:
            _log_compaction("Stage-1 trim", total_chars, stage1_total)
            await log_compaction_event(
                stage="trim",
                chars_before=total_chars,
                chars_after=stage1_total,
                session_id=session_id,
                agent_id=agent_id,
                run_id=run_id,
            )
            return stage1_ctx, recent_history_messages, None, {
                "stage": "trim",
                "chars_before": total_chars,
                "chars_after": stage1_total,
                "reduction_pct": stage1_savings_pct,
            }
        elif stage1_total <= threshold:
            _log.debug(
                f"⏭  Stage-1 would save only {stage1_savings_pct}% "
                f"(< {MIN_STAGE1_SAVINGS_PCT}% min) — falling through to Stage-2 LLM summarisation.",
            )

    # ── Stage 2: LLM summarisation (Stage 1 was not enough) ──────────────────────
    _log.debug(
        f"🗜️  Stage-1 insufficient — proceeding to LLM summarisation "
        f"({total_chars:,} chars, threshold {threshold:,})"
    )

    context_block = _build_context_block(current_context_text, recent_history_messages)
    if pin:
        context_block = ("=== THE REQUEST BEING WORKED ON (kept verbatim; do not restate it as a task) ===\n"
                         + pin + "\n" + context_block)

    archive_path = _make_archive_path(session_id)
    try:
        from core.secret_scrub import scrub_secrets
        from core.vault_backend import get_vault

        # Stored: a secret variable a step's prompt rendered in is kept by name (F854). Through the vault's own
        # write, so on a fleet it is in the object store and not only on this pod's disk (F1248).
        get_vault().write(archive_path, scrub_secrets(context_block))
        archive_path = _vault_key(archive_path)
        _log.debug(f"🗄️  Compaction archive saved → {archive_path}")
    except Exception as exc:
        _log.warning(f"⚠️  Could not write compaction archive: {exc}")
        archive_path = None

    try:
        from core.llm_providers import generate_response as llm_generate_response

        summary = await llm_generate_response(
            prompt_msg=context_block,
            sys_prompt=_COMPACTION_SYS_PROMPT,
            mode=mode,
            current_model=current_model,
            current_settings=current_settings,
            tools=None,
            history_messages=None,
            session_id=session_id,
            agent_id=agent_id,
            source="compaction",
        )

        # The key the vault's read tools take, never a scratch path (F1248).
        archive_hint = f" (the original is in the vault: {archive_path}; read it with read_file)" if archive_path else ""
        compacted = (
            f"{pin}"
            f"[CONTEXT COMPACTED{archive_hint}]\n"
            f"Original size: {total_chars:,} chars → compacted to ~{len(summary):,} chars\n\n"
            f"=== SUMMARY OF PRIOR CONTEXT ===\n"
            f"{summary}\n"
            f"=== END SUMMARY — CONTINUING FROM HERE ===\n"
        )
        # The floor: a summary that kept almost nothing keeps the newest
        # transcript verbatim with it — the observations the next step acts on
        # — and then the newest whole messages of the conversation, in the
        # space still left.
        floor = min(math.ceil(total_chars * MIN_KEEP_RATIO), threshold // 2)
        recent_header = "\n=== THE MOST RECENT CONTEXT, VERBATIM ===\n"
        room = floor - len(compacted) - len(recent_header) - 1
        if room > 0 and body:
            compacted += recent_header + body[-room:] + "\n"
        kept_history: list = []
        room = floor - len(compacted)
        for message in reversed(recent_history_messages or []):
            size = len(str(message.get("content", "")))
            if size > room:
                break
            kept_history.insert(0, message)
            room -= size
        while kept_history and kept_history[0].get("role") != "user":
            kept_history.pop(0)          # a conversation starts with its user
        after_chars = len(compacted) + sum(len(str(m.get("content", ""))) for m in kept_history)
        reduction_pct = round((total_chars - after_chars) / total_chars * 100) if total_chars > 0 else 0
        compact_stats = {
            "stage": "llm_summary",
            "chars_before": total_chars,
            "chars_after": after_chars,
            "reduction_pct": reduction_pct,
        }
        _log_compaction("Stage-2 LLM summary", total_chars, after_chars, str(archive_path) if archive_path else None)
        await log_compaction_event(
            stage="llm_summary",
            chars_before=total_chars,
            chars_after=after_chars,
            session_id=session_id,
            agent_id=agent_id,
            run_id=run_id,
            archive_path=str(archive_path) if archive_path else None,
            model=current_model,
        )
        return compacted, kept_history, str(archive_path) if archive_path else None, compact_stats

    except Exception as exc:
        _log.warning(f"⚠️  Compaction LLM call failed ({exc}), continuing without compaction")
        return current_context_text, recent_history_messages, None, None


def _log_compaction(stage: str, before: int, after: int, archive: str | None = None) -> None:
    saved = before - after
    pct = round(saved / before * 100) if before > 0 else 0
    sep = "─" * 52
    archive_line = f"\n  Archive  : {archive}" if archive else ""
    _log.debug(
        f"\n{sep}\n"
        f"DEBUG: 🗜️  COMPACTION — {stage}\n"
        f"DEBUG:   Before   : {before:>12,} chars  (~{before // 4:,} tokens est.)\n"
        f"DEBUG:   After    : {after:>12,} chars  (~{after // 4:,} tokens est.)\n"
        f"DEBUG:   Saved    : {saved:>12,} chars  (-{pct}%){archive_line}\n"
        f"DEBUG: {sep}\n",
    )
