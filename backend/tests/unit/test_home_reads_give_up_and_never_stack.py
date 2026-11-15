"""Home's reads give up after a while and are never stacked (F1612).

Home's reads were plain fetches with no time limit, so a stalled backend left every section empty for as long as the
stall lasted: no "could not load", no sign it was waiting, and the runs poll added another pending request every 10 s.
Now every Home read goes through `homeReads.ts`: `readJson` gives up after HOME_READ_TIMEOUT_MS (15 s, its reason said
where it is defined), so the section says it could not load; `oneAtATime` asks no read again while it is pending.
Run in node with the module's types stripped, as the editor's field checks are.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
READS = ROOT / "frontend/src/components/app/homeReads.ts"
DASHBOARD = ROOT / "frontend/src/components/app/Dashboard.tsx"


def _node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: Home's reads are TypeScript")
    assert READS.exists(), f"{READS.name} does not exist"
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                          f"import * as R from {json.dumps(str(READS))};\n{script}"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


#: A fetch that never answers, as a stalled backend's, until its signal gives up on it. Node lets a process end while
#: only `AbortSignal.timeout`'s timer is pending (a browser's page stays), so a timer holds it open meanwhile.
STALLED = """
const hold = setInterval(() => {}, 1000);
let asked = 0;
const stalled = (url, init) => { asked += 1; return new Promise((_, reject) => {
  init?.signal?.addEventListener('abort', () => reject(init.signal.reason), { once: true }); }); };
"""


def test_the_timeout_is_fifteen_seconds_and_says_why():
    out = _node("console.log(JSON.stringify({ms: R.HOME_READ_TIMEOUT_MS}));")
    assert out["ms"] == 15_000
    source = READS.read_text()
    head = source[:source.index("HOME_READ_TIMEOUT_MS")]
    assert "local" in head and "cold" in head, "the reason for 15 s is said where it is defined"


def test_a_stalled_read_gives_up():
    out = _node(STALLED + """
const t0 = Date.now();
let why = null;
try { await R.readJson('/api/usage/summary', { timeoutMs: 50, fetchImpl: stalled }); } catch (e) { why = e?.name; }
clearInterval(hold);
console.log(JSON.stringify({why, ms: Date.now() - t0}));
""")
    assert out["why"] == "TimeoutError" and out["ms"] < 2000, out


def test_a_pending_read_is_not_asked_again_and_is_once_it_has_given_up():
    out = _node(STALLED + """
const read = R.oneAtATime(() => R.readJson('/api/orchestrations/runs', { timeoutMs: 50, fetchImpl: stalled }));
const first = read();
const second = read();
let gaveUp = false;
try { await first; } catch { gaveUp = true; }
const third = read();
third?.catch(() => {});
clearInterval(hold);
console.log(JSON.stringify({second: second === null, gaveUp, asked}));
""")
    assert out == {"second": True, "gaveUp": True, "asked": 2}, out


def test_home_reads_through_them():
    source = DASHBOARD.read_text()
    assert "from './homeReads'" in source, "Dashboard imports Home's reads"
    assert "fetch(`/api/orchestrations/runs?limit" not in source and "fetch(url)" not in source, \
        "no Home read is a bare fetch"
