"""
The combined image builds its web once, natively, for every architecture (F1572).

2.0.8's combined image (`Dockerfile`, `synapseorchai/synapse-ai`) timed out twice at the release job's 90 minutes, stuck
at `[linux/arm64 frontend-builder 4/6] RUN npm ci`: buildx ran the web's stage once per target, and arm64's under QEMU.
2.0.6 and 2.0.7 took about 28 minutes on a warm cache that has since expired.

The web's build is JavaScript, the same for every architecture, so its stage runs on the builder's own platform
(`--platform=$BUILDPLATFORM`), once, and both images copy its output. The one native module the standalone output
carries is sharp, for Next's image optimizer, which the app's pages never ask for (they use plain `<img>`); built on
amd64, an arm64 image's sharp loads its WebAssembly build, `@img/sharp-wasm32`, which the lock installs everywhere.
Every stage an image runs keeps the target's platform: the Python, Node and system packages are the target's.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

_FROM = re.compile(r"^FROM\s+(?:--platform=(\S+)\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.M | re.I)


def _stages(dockerfile: str) -> list[dict]:
    text = (REPO / dockerfile).read_text(encoding="utf-8")
    return [{"platform": p or None, "image": i, "name": n} for p, i, n in _FROM.findall(text)]


def test_the_combined_images_web_is_built_on_the_builders_own_platform():
    web = next(s for s in _stages("Dockerfile") if s["name"] == "frontend-builder")
    assert web["platform"] == "$BUILDPLATFORM", web


def test_every_stage_an_image_runs_is_the_targets():
    for dockerfile in ("Dockerfile", "Dockerfile.backend", "Dockerfile.worker"):
        stages = _stages(dockerfile)
        assert stages, dockerfile
        # The last stage is the image; no other stage but the web's is taken off the target's platform.
        assert stages[-1]["platform"] is None, (dockerfile, stages[-1])
        assert all(s["platform"] is None for s in stages if s["name"] != "frontend-builder"), (dockerfile, stages)


def test_the_combined_image_takes_only_the_webs_built_output():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    taken = re.findall(r"^COPY --from=frontend-builder (\S+) ", text, re.M)
    assert taken == ["/app/.next/standalone", "/app/.next/static", "/app/public"], taken


def test_sharp_can_load_on_any_architecture():
    lock = json.loads((REPO / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert "node_modules/@img/sharp-wasm32" in lock["packages"]
