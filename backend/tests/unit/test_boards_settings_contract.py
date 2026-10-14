"""Boards' settings (BOARD1a, D280): one gate, off by default, the same keys and values everywhere."""
from __future__ import annotations

from core.boards import config as bconfig
from core.config import default_settings, load_settings
from core.models import Settings


def test_the_engine_defaults_carry_every_key_with_the_same_value():
    for key, value in bconfig.DEFAULTS.items():
        assert default_settings()[key] == value, key
        assert Settings.model_fields[key].default == value, key


def test_off_by_default():
    assert bconfig.boards_enabled({}) is False
    assert bconfig.boards_enabled(load_settings()) is False


def test_a_missing_or_null_value_reads_as_its_default():
    knobs = bconfig.board_settings({"boards_enabled": True, "boards_max_open_cards": None})
    assert knobs.enabled is True and knobs.max_open_cards == 2000 and knobs.max_shared == 0
