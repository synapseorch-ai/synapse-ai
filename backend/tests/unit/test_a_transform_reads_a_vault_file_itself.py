"""A Transform reads a Vault file itself, never a reader's JSON answer as the file (F1227, CAP3's stage 1).

Brief 4 ("work out last week's sales by region from this CSV") built a `vault_read` step and a Transform that read
`state.read_csv` into `csv.DictReader`. That state holds the tool's answer, `{"path": …, "content": …}`, so the reader
found no rows and the board said $0.00 for every region. Now Transform code reads a file with `read_vault("path")`:
the engine reads each path written out in the code through the vault tools' own read (the Vault's path rule, the blob
store a host overlays, their size limit) before the step runs and hands the text in beside `state`, so the same code
works in Docker, on the host and on a fleet's Lambda, which mounts no Vault. The builder's check refuses only code that
parses the wrapper as the file; reading its `['content']` stays right. A `read_vault` whose path is not written out is
refused too: the step could not read it first.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from _fakes import seed as S

from core.execution.scripts import vault_paths_named, wrap_transform

from tests.unit.test_orchestration_stepcov import _run

CSV = "date,region,amount\n2026-10-20,North,100.50\n2026-10-21,South,40\n2026-10-22,North,9.50\n"

#: Brief 4's own Transform, as stage 1 saved it (cut to the part that read the file).
BRIEF_4 = """import csv
import io

csv_content = state.read_csv
csv_file = io.StringIO(csv_content)
reader = csv.DictReader(csv_file)
totals = {}
for row in reader:
    totals[row['region']] = totals.get(row['region'], 0) + float(row['amount'])
result = totals
"""


def _orch(code: str) -> dict:
    return {"name": "Sales", "entry_step_id": "read", "steps": [
        {"id": "read", "name": "Read", "type": "tool", "forced_tool": "vault_read",
         "tool_arguments": {"path": "cap3/sales.csv"}, "output_key": "read_csv", "next_step_id": "sum"},
        {"id": "sum", "name": "Sum", "type": "transform", "transform_code": code, "output_key": "totals",
         "next_step_id": "end"},
        {"id": "end", "name": "End", "type": "end"}]}


def _issues(code: str) -> list[str]:
    from core.builder_tools import _validate_orchestration

    return [i for i in _validate_orchestration(_orch(code))["issues"] if "Transform step 'sum'" in i]


# ── the builder's check ──────────────────────────────────────────────────────────────────────────────────────────
class TestTheCheck:
    def test_brief_4s_transform_is_refused_naming_the_file(self):
        [said] = _issues(BRIEF_4)
        assert "read_vault('cap3/sales.csv')" in said and "'state.read_csv'" in said

    @pytest.mark.parametrize("parse", [
        "rows = list(csv.reader(state['read_csv']))",
        "rows = csv.DictReader(io.StringIO(state.get('read_csv')))",
        "import pandas as pd\nframe = pd.read_csv(io.StringIO(state.read_csv))",
        "lines = state['read_csv'].splitlines()",
        "text = state.read_csv\nlines = text.split('\\n')",
    ])
    def test_any_raw_parse_of_the_wrapper(self, parse):
        assert _issues("import csv, io\n" + parse + "\nresult = 1")

    @pytest.mark.parametrize("right", [
        "import json, csv, io\nrows = list(csv.DictReader(io.StringIO(json.loads(state.read_csv)['content'])))\nresult = rows",
        "import json\ndata = json.loads(state['read_csv'])\nresult = data['content'].splitlines()",
        "result = state['read_csv']['content'].splitlines()",
        "import csv, io\nresult = list(csv.DictReader(io.StringIO(read_vault('cap3/sales.csv'))))",
        "result = state['n'] * 2",
    ])
    def test_reading_the_content_or_the_file_stays_right(self, right):
        assert _issues(right) == []

    def test_a_read_vault_whose_path_is_not_written_out_is_refused(self):
        [said] = _issues("where = 'cap3/' + 'sales.csv'\nresult = read_vault(where)")
        assert "name the file; read_vault needs the path written out" in said

    def test_the_paths_written_out_are_what_the_step_reads(self):
        code = "a = read_vault('cap3/sales.csv')\nb = read_vault(\"/cap3/notes.md\")\nc = read_vault(x)"
        assert vault_paths_named(code) == (["cap3/sales.csv", "cap3/notes.md"], [3])


# ── the script: read_vault hands the file's text in, and only the files named ───────────────────────────────────
def _python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)


class TestTheScript:
    def test_read_vault_gives_the_file_s_text(self):
        out = _python(wrap_transform("result = read_vault('cap3/sales.csv').splitlines()[0]", {},
                                     {"cap3/sales.csv": CSV}))
        assert out.returncode == 0, out.stderr
        assert '"result": "date,region,amount"' in out.stdout

    def test_a_file_not_named_says_how_to_name_it(self):
        out = _python(wrap_transform("p = 'x.csv'\nresult = read_vault(p)", {}, {}))
        assert "FileNotFoundError" in out.stderr and "only a path written out in the code is read" in out.stderr

    def test_text_with_quotes_and_backslashes_arrives_unchanged(self):
        hostile = 'a """ b \\ c \'\'\' d\n'
        out = _python(wrap_transform("result = read_vault('f.txt') == " + repr(hostile), {}, {"f.txt": hostile}))
        assert '"result": true' in out.stdout, out.stderr


# ── the engine: a Transform step reads the Vault through the vault tools' own read ──────────────────────────────
@pytest.fixture
def host_runtime(monkeypatch):
    import core.config as config

    monkeypatch.setattr(config, "load_settings", lambda: {"model": "mistral"})
    monkeypatch.setenv("SYNAPSE_TRANSFORM_RUNTIME", "host")


def _step(code: str) -> dict:
    return S.make_orchestration(entry_step_id="t", steps=[
        {"id": "t", "name": "Sum", "type": "transform", "transform_code": code, "output_key": "totals",
         "timeout_seconds": 60, "next_step_id": None}])


class TestTheEngine:
    async def test_the_totals_come_out_right(self, host_runtime):
        from core.storage import get_blob_store

        get_blob_store().put("vault/cap3/sales.csv", CSV)
        code = ("import csv, io\ntotals = {}\nfor row in csv.DictReader(io.StringIO(read_vault('cap3/sales.csv'))):\n"
                "    totals[row['region']] = totals.get(row['region'], 0) + float(row['amount'])\nresult = totals")
        events = await _run(_step(code))
        [done] = [e for e in events if e.get("type") == "transform_result"]
        assert done["result"] == str({"North": 110.0, "South": 40.0})

    async def test_a_missing_file_fails_the_step_naming_it(self, host_runtime):
        events = await _run(_step("result = read_vault('cap3/nowhere.csv')"))
        said = " ".join(str(e.get("response") or e.get("error") or "") for e in events)
        assert "read_vault('cap3/nowhere.csv')" in said and "Not found" in said

    async def test_a_path_out_of_the_vault_is_refused(self, host_runtime):
        events = await _run(_step("result = read_vault('../../etc/passwd')"))
        said = " ".join(str(e.get("response") or e.get("error") or "") for e in events)
        assert "Not a vault path" in said
