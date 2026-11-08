"""A parsing child that misbehaves on request, for `runner.run_parse(command=[python, this file])`.

`job.json`'s `fake` field says how: `sleep` (save progress, then hang), `allocate`
(grow without bound), `crash` (save progress, then die), `crash_early` (die
with nothing saved), `env` (answer a document listing the environment it got),
`count` (tell 1 of 4 pages read, then a torn count, then 3 of 4, then answer).
Run by path, so it imports only what the real child's PYTHONPATH reaches.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _doc(text: str, *, cut: bool) -> str:
    from core.knowledge.parse.tree import DocBuilder, source_info
    from core.knowledge.parsed import ParserInfo

    b = DocBuilder()
    b.page(1)
    b.page(2)
    b.add("paragraph", text, page=1)
    if cut:
        b.cut("truncated_pages")
    return b.build(source_info(path="kb/a.pdf", sha256="0" * 64, size=1, mime="x", source="vault"),
                   ParserInfo(name="fake", version="1")).model_dump_json()


def main(workdir: str) -> None:
    root = Path(workdir)
    fake = json.loads((root / "job.json").read_text())["fake"]
    if fake in ("sleep", "crash", "allocate"):
        (root / "partial.json").write_text(_doc("Page one was read.", cut=True))
    if fake == "sleep":
        time.sleep(60)
    elif fake == "allocate":
        hog = []
        while True:
            hog.append(bytearray(32 * 1024 * 1024))
            time.sleep(0.05)
    elif fake in ("crash", "crash_early"):
        print("Traceback (most recent call last):\nValueError: the parser fell over", file=sys.stderr)
        os._exit(3)
    elif fake == "env":
        (root / "result.json").write_text(_doc(" ".join(sorted(os.environ)), cut=False))
    elif fake == "count":
        (root / "progress.json").write_text(json.dumps({"done": 1, "total": 4}))
        time.sleep(1.3)
        (root / "progress.json").write_text('{"done": 3, "tot')  # torn: a reader never takes it for a count
        time.sleep(1.3)
        (root / "progress.json").write_text(json.dumps({"done": 3, "total": 4}))
        time.sleep(1.3)
        (root / "result.json").write_text(_doc("Four pages were read.", cut=False))


if __name__ == "__main__":
    main(sys.argv[1])
