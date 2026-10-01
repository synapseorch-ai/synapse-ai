"""The parsing subprocess: `python -m core.knowledge.parse.child <workdir>`.

`runner.py` writes `job.json` and `input.bin` into a fresh directory and starts
this module with an allowlisted environment. It answers with exactly one of:

* `result.json` — the `ParsedDocument`;
* `error.json` — a refusal (`unsupported` or `refused`, with status and reason);
* nothing, because it was killed or crashed — then `partial.json`, the last
  checkpoint a paged reader saved, is what was read before the cut.

Pages without a text layer are rendered to `pages/<n>.png` when the job asks
for OCR. The parent reads them; this process never calls a seam (none is
registered here, and it holds no credentials to call one with).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: A paged reader's progress is saved at most this often.
CHECKPOINT_S = 10.0


def main(workdir: str) -> int:
    from core.knowledge.parse import UnsupportedFormat, parse_bytes
    from core.knowledge.parse.limits import ParseRefused

    root = Path(workdir)
    job = json.loads((root / "job.json").read_text("utf-8"))
    data = (root / "input.bin").read_bytes()
    pages_dir = root / "pages"
    last = [0.0]

    def render(page: int, png: bytes) -> None:
        pages_dir.mkdir(exist_ok=True)
        (pages_dir / f"{page}.png").write_bytes(png)

    def checkpoint(build) -> None:
        now = time.monotonic()
        if now - last[0] < CHECKPOINT_S:
            return
        last[0] = now
        _write(root / "partial.json", build().model_dump_json())

    try:
        parsed = parse_bytes(
            data, path=job["path"], source=job["source"], sha256=job["sha256"], mime=job["mime"],
            url=job.get("url"), fetched_at=job.get("fetched_at"), etag=job.get("etag"),
            modified_at=job.get("modified_at"), max_pages=job.get("max_pages"), ocr=bool(job.get("ocr")),
            docling=bool(job.get("docling")), render=render if job.get("ocr") else None, checkpoint=checkpoint)
    except UnsupportedFormat as exc:
        _write(root / "error.json", json.dumps({"kind": "unsupported", "message": str(exc)}))
        return 0
    except ParseRefused as exc:
        _write(root / "error.json", json.dumps({"kind": "refused", "status": exc.status, "reason": exc.reason,
                                                "message": str(exc)}))
        return 0
    _write(root / "result.json", parsed.model_dump_json())
    return 0


def _write(path: Path, text: str) -> None:
    """Whole or not at all: a reader of `path` never sees half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


if __name__ == "__main__":
    if hasattr(os, "nice"):
        try:
            os.nice(5)  # parsing yields to the server it runs beside
        except OSError:
            pass
    sys.exit(main(sys.argv[1]))
