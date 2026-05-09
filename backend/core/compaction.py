"""
Auto context compaction: when accumulated context exceeds a configurable threshold,
ask the LLM to summarise everything to ~30% of its original size, archive the
full original to the vault, and return the compact summary so the agent can continue.

Two-stage strategy (mirrors Claude Code's approach):
  Stage 1 — cheap trim: drop the oldest portion of current_context_text to fit
             within the threshold. No LLM call; no archive.
  Stage 2 — LLM summarise: if Stage 1 alone is not enough (e.g. history is large),
             call the LLM to write a dense summary and archive the full original.
"""
import re
from datetime import datetime
from pathlib import Path

from core.config import DATA_DIR
from core.usage_tracker import log_compaction_event

COMPACT_ARCHIVE_DIR = Path(DATA_DIR) / "vault" / "compaction_archives"

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
    "and key-value pairs over prose. Never include filler phrases like 'The assistant then…'"
)


def _make_archive_path(session_id: str) -> Path:
    COMPACT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
    safe_id = re.sub(r"[^\w]", "_", session_id or "session")[:40]
    return COMPACT_ARCHIVE_DIR / f"{safe_id}_{timestamp}.txt"


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
) -> tuple[str, list, str | None, dict | None]:
    """
    Returns (context_text, history_messages, archive_path | None, compact_stats | None).

    archive_path is only set when Stage-2 LLM summarisation fires (and saves an archive).
    compact_stats is {"stage", "chars_before", "chars_after", "reduction_pct"} when
    compaction fired, or None when it didn't.
    """
    if not settings.get("auto_compact_enabled", False):
        return current_context_text, recent_history_messages, None, None

    threshold = settings.get("auto_compact_threshold", 100000)
    history_chars = sum(
        len(str(m.get("content", ""))) for m in (recent_history_messages or [])
    )
    total_chars = len(current_context_text) + history_chars

    if total_chars <= threshold:
        return current_context_text, recent_history_messages, None, None

    print(
        f"\nDEBUG: 📏 Compaction check — "
        f"context {total_chars:,} chars (~{total_chars // 4:,} tokens est.) "
        f"exceeds threshold {threshold:,} chars (~{threshold // 4:,} tokens est.). "
        f"history={history_chars:,} tool_ctx={len(current_context_text):,}. "
        f"Attempting Stage-1 trim…",
        flush=True,
    )

    # ── Stage 1: drop oldest tool outputs (cheap — no LLM call, no archive) ──────
    # Keep the most recent portion of current_context_text that fits in the budget.
    # Only accept the Stage-1 result when it saves at least MIN_STAGE1_SAVINGS_PCT;
    # otherwise fall through to Stage-2 LLM summarisation for proper ~30% reduction.
    budget = max(0, threshold - history_chars - 60)  # 60 chars for the trim prefix
    if budget > 0 and len(current_context_text) > budget:
        stage1_ctx = "[...older tool outputs trimmed...]\n" + current_context_text[-budget:]
        stage1_total = len(stage1_ctx) + history_chars
        stage1_savings_pct = round((total_chars - stage1_total) / total_chars * 100) if total_chars > 0 else 0
        if stage1_total <= threshold and stage1_savings_pct >= MIN_STAGE1_SAVINGS_PCT:
            _log_compaction("Stage-1 trim", total_chars, stage1_total)
            log_compaction_event(
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
            print(
                f"DEBUG: ⏭  Stage-1 would save only {stage1_savings_pct}% "
                f"(< {MIN_STAGE1_SAVINGS_PCT}% min) — falling through to Stage-2 LLM summarisation.",
                flush=True,
            )

    # ── Stage 2: LLM summarisation (Stage 1 was not enough) ──────────────────────
    print(
        f"DEBUG: 🗜️  Stage-1 insufficient — proceeding to LLM summarisation "
        f"({total_chars:,} chars, threshold {threshold:,})"
    )

    context_block = _build_context_block(current_context_text, recent_history_messages)

    archive_path = _make_archive_path(session_id)
    try:
        archive_path.write_text(context_block, encoding="utf-8")
        print(f"DEBUG: 🗄️  Compaction archive saved → {archive_path}")
    except Exception as exc:
        print(f"DEBUG: ⚠️  Could not write compaction archive: {exc}")
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

        archive_hint = f" (original archived at: {archive_path})" if archive_path else ""
        compacted = (
            f"[CONTEXT COMPACTED{archive_hint}]\n"
            f"Original size: {total_chars:,} chars → compacted to ~{len(summary):,} chars\n\n"
            f"=== SUMMARY OF PRIOR CONTEXT ===\n"
            f"{summary}\n"
            f"=== END SUMMARY — CONTINUING FROM HERE ===\n"
        )
        after_chars = len(compacted)
        reduction_pct = round((total_chars - after_chars) / total_chars * 100) if total_chars > 0 else 0
        compact_stats = {
            "stage": "llm_summary",
            "chars_before": total_chars,
            "chars_after": after_chars,
            "reduction_pct": reduction_pct,
        }
        _log_compaction("Stage-2 LLM summary", total_chars, after_chars, str(archive_path) if archive_path else None)
        log_compaction_event(
            stage="llm_summary",
            chars_before=total_chars,
            chars_after=after_chars,
            session_id=session_id,
            agent_id=agent_id,
            run_id=run_id,
            archive_path=str(archive_path) if archive_path else None,
            model=current_model,
        )
        return compacted, [], str(archive_path) if archive_path else None, compact_stats

    except Exception as exc:
        print(f"DEBUG: ⚠️  Compaction LLM call failed ({exc}), continuing without compaction")
        return current_context_text, recent_history_messages, None, None


def _log_compaction(stage: str, before: int, after: int, archive: str | None = None) -> None:
    saved = before - after
    pct = round(saved / before * 100) if before > 0 else 0
    sep = "─" * 52
    archive_line = f"\n  Archive  : {archive}" if archive else ""
    print(
        f"\nDEBUG: {sep}\n"
        f"DEBUG: 🗜️  COMPACTION — {stage}\n"
        f"DEBUG:   Before   : {before:>12,} chars  (~{before // 4:,} tokens est.)\n"
        f"DEBUG:   After    : {after:>12,} chars  (~{after // 4:,} tokens est.)\n"
        f"DEBUG:   Saved    : {saved:>12,} chars  (-{pct}%){archive_line}\n"
        f"DEBUG: {sep}\n",
        flush=True,
    )
