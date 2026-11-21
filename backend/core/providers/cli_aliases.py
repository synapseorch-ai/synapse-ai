"""What a model CLI's own alias runs (F1598 D).

Claude Code takes aliases beside full model ids: `haiku`, `sonnet`, `opus` and `fable`, each the model of its tier it
runs today. Facts are kept by model id (`model_facts`, `windows`), so an alias is read as the model it runs: its tier,
its context window and how it ranks are that model's, and a picker says them under it.

The table is what Claude Code answered when asked: `claude -p --model <alias> --output-format json` names the model
that answered in its `modelUsage` (beside its own side calls on a small model). When Claude Code moves an alias, this
table moves with it; until then an alias reads as the model it last ran here.
"""
from __future__ import annotations

#: Claude Code's aliases and the model each runs.
CLAUDE_CODE = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
               "fable": "claude-fable-5-1"}


def resolved(model_id: str) -> str | None:
    """`cli.claude.<alias>` as the model it runs (`cli.claude.<model id>`); None for anything else."""
    head, dot, alias = (model_id or "").strip().lower().rpartition(".")
    if head != "cli.claude" or not dot:
        return None
    target = CLAUDE_CODE.get(alias)
    return f"cli.claude.{target}" if target else None
