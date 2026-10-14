"""Boards' settings, read in one place, and the one gate: `boards_enabled()` (BOARD1a, D280).

The keys live in `core.config._DEFAULTS` and the `Settings` model like every other setting. **Boards are off by
default** (C1a): while they are off, no board tool, digest or prompt line reaches any turn, and no board query
runs — the engine asks this gate before anything else, so a pin that carries the tables into cloud before its
screens is dark there too. Cloud overlays its platform switch and the plan's caps through its settings
provider (BOARD1b), as it does Knowledge's.

The run scope (a team's task list, TEAM1a) does not ask this gate: it works with Boards off (Q1).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The keys and their shipped defaults. `core.config._DEFAULTS` carries the same values; a test holds the two equal.
DEFAULTS: dict[str, Any] = {
    "boards_enabled": False,
    # Open cards per board (C6: by plan in cloud, e.g. 200 on Free up to 2,000).
    "boards_max_open_cards": 2000,
    # Shared boards per workspace (C8: by plan in cloud); 0 is no limit.
    "boards_max_shared": 0,
}

#: The digest's card lines, across every board a turn has (C6).
DIGEST_LINES = 40


@dataclass(frozen=True)
class BoardSettings:
    enabled: bool
    max_open_cards: int
    max_shared: int


def _get(settings: Mapping[str, Any], key: str) -> Any:
    value = settings.get(key)
    return DEFAULTS[key] if value is None else value


def board_settings(settings: Mapping[str, Any] | None = None) -> BoardSettings:
    """The typed view of the current (or given) settings."""
    if settings is None:
        from core.config import load_settings
        settings = load_settings()
    return BoardSettings(
        enabled=bool(_get(settings, "boards_enabled")),
        max_open_cards=max(0, int(_get(settings, "boards_max_open_cards"))),
        max_shared=max(0, int(_get(settings, "boards_max_shared"))),
    )


def boards_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    """The gate. While false no board tool, digest, prompt line or query reaches a turn."""
    return board_settings(settings).enabled
