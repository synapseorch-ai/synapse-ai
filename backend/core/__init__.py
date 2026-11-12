"""The engine.

Importing any of it first turns third-party libraries' usage tracking off, before a library that phones home can
load (F1567): `core.library_tracking`.
"""
from . import library_tracking as _library_tracking

_library_tracking.turn_off()
