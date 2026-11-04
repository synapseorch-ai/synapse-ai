"""A code step that reads an object as text is told what to read instead (F1359's engine half, TRI1; CAP3's FF18 gate,
cap-05; Naveen 11-04: "Build as planned").

cap-05's built Transform ran `re.search(r'(SO-\\d+)', state.receive_request)`, where `receive_request` was a Human
step's answer, the object `{approve, approved, feedback}`, and the run failed with "TypeError: expected string or
bytes-like object, got '_SynapseState'": the state class's name, and nothing a person or the planner can act on. The
failure still names the line and what it raised, and now says that a value read from `state` is an object there, and to
read one of its keys. Any other failure reads as it did.

A string method called on one (`.split`) is told the same by the state class itself, its keys named. Run for real: the
wrapped script written to `script.py` in a subprocess, as a runner runs it, its stderr read back by `transform_failure`.
"""
from __future__ import annotations

import subprocess
import sys


def _fails(code: str, state: dict, tmp_path) -> str:
    from core.execution.scripts import transform_failure, transform_line_offset, wrap_transform

    script = tmp_path / "script.py"
    script.write_text(wrap_transform(code, state))
    ran = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert ran.returncode != 0, ran.stdout
    return transform_failure(ran.stderr, code, transform_line_offset(state), ran.returncode)


CAP_05 = "import re\ntext = state.receive_request\norder_match = re.search(r'(SO-\\d+)', text)\nresult = order_match"
STATE = {"user_input": "Refund SO-2077, 45.00, damaged in transit",
         "receive_request": {"approve": True, "approved": True, "feedback": "approve"}}


def test_cap_05s_code_names_its_line_and_says_to_read_a_key(tmp_path):
    said = _fails(CAP_05, STATE, tmp_path)
    assert said.startswith("Transform failed on line 3 (`order_match = re.search(r'(SO-\\d+)', text)`): TypeError"), said
    assert "a value read from `state` is an object here, not text: read one of its keys" in said


def test_a_string_method_on_an_object_is_told_the_same(tmp_path):
    said = _fails("result = state.receive_request.split(',')", STATE, tmp_path)
    assert "is an object, not text, so it has no 'split'" in said and "approve, approved, feedback" in said, said


def test_any_other_failure_reads_as_it_did(tmp_path):
    said = _fails("x = 1\nraise ValueError('boom')", STATE, tmp_path)
    assert said == "Transform failed on line 2 (`raise ValueError('boom')`): ValueError: boom"


def test_a_missing_key_named_like_a_word_is_still_a_missing_key(tmp_path):
    """`title`, `count`, `index` are keys a workflow writes; only a method text alone has is read as one."""
    said = _fails("result = state.receive_request.title", STATE, tmp_path)
    assert "no key 'title' here; the keys are: approve, approved, feedback" in said and "not text" not in said, said
