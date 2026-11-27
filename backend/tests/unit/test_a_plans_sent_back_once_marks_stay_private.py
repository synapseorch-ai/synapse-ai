"""A plan check's once-a-run marks never reach a public read of the builder's run (F1753, F1754).

The checks a plan goes back for once a run (`approved_sends`: a model-filled send, F1743; a field a send reads that
starts empty, F1753; a value a send sends that its approval never shows, F1754) keep that they asked in the builder
run's state, under a `_` key. A public read of a run's state drops the engine's own keys by name
(`events_public.PRIVATE_STATE_PREFIXES`), and the walker that holds that list finds a key by its constant's name at
the read; `approved_sends` reads its marks in one loop, by a variable, which the walker cannot follow. So every mark
the module names is held here: none reaches a public read.
"""
from __future__ import annotations

from core.events_public import public_state
from core.native_builder import approved_sends


def test_every_mark_the_module_names_is_kept_out_of_public_reads():
    marks = {name: value for name, value in vars(approved_sends).items()
             if name.endswith("SAID") and isinstance(value, str)}
    assert set(marks) >= {"SAID", "EMPTY_SAID", "UNSHOWN_SAID"}, marks
    assert public_state({**{v: True for v in marks.values()}, "draft": {"to": "robin@cedarline.example"}}) == {
        "draft": {"to": "robin@cedarline.example"}}
