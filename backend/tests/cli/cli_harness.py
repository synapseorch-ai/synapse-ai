"""Driving `synapse` the way a person or a script does, for the CLI's tests (D126, D294).

`main()` runs the real argument parser and command in this process and hands
back (exit code, stdout, stderr); `answers()` is a person at the terminal.
The fixtures that use these live in this folder's `conftest.py`.
"""
from __future__ import annotations

import builtins
import sys

import pytest

cli = pytest.importorskip("synapse.cli", reason="the synapse package is not importable here")
from synapse.client import commands  # noqa: E402


def main(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    capsys.readouterr()  # only this command's output: not a fixture's, nor the app's own prints before it
    monkeypatch.setattr(sys, "argv", ["synapse", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def answers(monkeypatch, *replies):
    """A person at the terminal, typing these replies in order."""
    it = iter(replies)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    monkeypatch.setattr(commands, "_interactive", lambda: True)
