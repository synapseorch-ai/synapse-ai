"""Boards — cards of shared work that agents in chats, workflows and schedules read, claim and resolve
(BOARD1a, D280; MEMORY 20 §5). Off by default (`config.boards_enabled`).

* `config` — the gate and the caps;
* `resolve` — which boards a turn has, from where it runs, and releasing a finished turn's claims;
* `tools` — the five tools, the digest, and what each call says back.

The store is `core/store/boards.py`. This module imports nothing, so `core.visitor` (which cloud's API reads)
can name the tools without loading the store.
"""

TOOL_NAMES = ("board_list", "board_read", "board_add", "board_update", "board_claim")
