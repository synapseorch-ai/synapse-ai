"""One agent step's context budget, from its model's window (D339 P1).

An agent holds its work in one text transcript. Two fixed character limits kept it: a tool result over
`vault_threshold` (100,000) went to the vault, and a transcript over `auto_compact_threshold` (80,000, about 20k
tokens — a tenth of Haiku 4.5's window) was trimmed or summarised. Neither knew the model, and the spill line sat
above the compaction line, so one result under it could set off a compaction alone (TRI1's triage, 10-29).

Now the budget is a share of the model's own input window (`auto_compact_window_pct`, 50 by default; Naveen,
10-30), and every line is drawn from it:

* **tidy** past ~15% of it: older tool results go to the vault, the newest three kept whole, in batches;
* **compact at** the budget;
* **spill** one result over ~15% of it to the vault;
* **read** at most ~10% of it per vault read — under the spill line, so a read is never vaulted again;
* **cut** the prompt (the engine's last-resort trim) at 90% of the window.

A model whose window is not known (`core.providers.window_tokens` says None), or a share of 0, keeps the
workspace's two fixed limits, as before.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The engine's own estimate (`core.knowledge.profiles`, `core.team`).
CHARS_PER_TOKEN = 4
#: The share of the window a step may fill before it compacts, in percent (Naveen, 10-30).
DEFAULT_WINDOW_PCT = 50
#: One result over this share of the budget is vaulted; a vault read answers at most the second.
SPILL_SHARE, READ_SHARE = 0.15, 0.10
#: Past this share of the budget, older tool results go to the vault in batches (the tidy line; Naveen, 10-30).
TIDY_SHARE = 0.15
#: Bounds on those, in characters: a 1M-token window still reads a page at a time, and a small one still holds one.
SPILL_MIN, SPILL_MAX = 8_000, 200_000
READ_MIN, READ_MAX = 4_000, 50_000
#: A budget is never below this, in characters, whatever the window.
COMPACT_MIN = 16_000
#: The prompt's last-resort cut with no window known (`react_engine`'s former `MAX_PROMPT_CHARS`).
PROMPT_MAX_FIXED = 400_000
#: Of the window, what the last-resort cut leaves for the prompt.
PROMPT_SHARE = 0.9


@dataclass(frozen=True)
class Budget:
    #: The model's input window in tokens; None when not known.
    window: int | None
    #: Characters: compaction's threshold, one result's spill line, one vault read's cap, the prompt's cut, and the
    #: tidy line past which older tool results move to the vault (the threshold itself with fixed limits).
    compact_at: int
    spill_at: int
    read_cap: int
    prompt_max: int
    tidy_at: int
    #: "window" (drawn from it) or "fixed" (the workspace's two limits).
    source: str


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def budget(model: str, settings: dict, *, output_tokens: int | None = None) -> Budget:
    """The budget an agent step on `model` works within, under this workspace's `settings`."""
    from core.llm_providers import DEFAULT_MAX_OUTPUT_TOKENS
    from core.providers import window_tokens

    fixed_compact = _int(settings.get("auto_compact_threshold"), 80_000)
    fixed_spill = _int(settings.get("vault_threshold"), 100_000)
    pct = max(0, min(90, _int(settings.get("auto_compact_window_pct", DEFAULT_WINDOW_PCT), DEFAULT_WINDOW_PCT)))
    window = window_tokens(model) if pct else None
    if not window:
        return Budget(window=None, compact_at=fixed_compact, spill_at=fixed_spill,
                      read_cap=max(READ_MIN, min(READ_MAX, fixed_spill // 2)), prompt_max=PROMPT_MAX_FIXED,
                      tidy_at=fixed_compact, source="fixed")
    reserve = output_tokens or _int(settings.get("max_output_tokens"), DEFAULT_MAX_OUTPUT_TOKENS)
    usable = max(1_000, window - reserve)
    chars = usable * pct // 100 * CHARS_PER_TOKEN
    spill = max(SPILL_MIN, min(SPILL_MAX, int(chars * SPILL_SHARE)))
    return Budget(
        window=window,
        compact_at=max(COMPACT_MIN, chars),
        spill_at=spill,
        read_cap=max(READ_MIN, min(READ_MAX, int(chars * READ_SHARE), spill // 2)),
        prompt_max=max(COMPACT_MIN, int(usable * PROMPT_SHARE) * CHARS_PER_TOKEN),
        tidy_at=max(COMPACT_MIN // 2, int(chars * TIDY_SHARE)),
        source="window",
    )
