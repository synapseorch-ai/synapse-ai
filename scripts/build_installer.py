#!/usr/bin/env python3
"""
Build dist/synapse-install.pyz: the installer as one file, for a release.

setup.sh and setup.ps1 download it from the GitHub release and run it with a
Python uv provides. It is a zipapp of `synapse/installer/` (standard library
only), stamped with the version it installs (`BUILT_FOR`) and carrying the
pinned dependency set (`constraints.txt`, from backend/requirements-wheel.lock
when present), so a release's installer always installs that release, resolved
the way CI tested it.

    python scripts/build_installer.py [--out dist] [--version X]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)


def build(out_dir: Path, version: str) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        package = stage / "synapse" / "installer"
        package.mkdir(parents=True)
        (stage / "synapse" / "__init__.py").write_text("", encoding="utf-8")
        for source in (ROOT / "synapse" / "installer").glob("*.py"):
            shutil.copy2(source, package / source.name)
        init = package / "__init__.py"
        stamped = re.sub(r'^BUILT_FOR = ".*"$', f'BUILT_FOR = "{version}"',
                         init.read_text(encoding="utf-8"), flags=re.M)
        assert f'BUILT_FOR = "{version}"' in stamped, "BUILT_FOR line not found"
        init.write_text(stamped, encoding="utf-8")
        lock = ROOT / "backend" / "requirements-wheel.lock"
        if lock.exists():
            shutil.copy2(lock, package / "constraints.txt")
        (stage / "__main__.py").write_text(
            "import sys\nfrom synapse.installer.__main__ import main\nsys.exit(main())\n",
            encoding="utf-8",
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "synapse-install.pyz"
        zipapp.create_archive(stage, target=target, compressed=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out_dir / "synapse-install.pyz.sha256").write_text(f"{digest}  synapse-install.pyz\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    target = build(Path(args.out), args.version or project_version())
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
