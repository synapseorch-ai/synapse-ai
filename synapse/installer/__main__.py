"""`python synapse-install.pyz [install|upgrade] [--yes] …` — see flow.py.

Every option a user may give has an environment twin, because setup.ps1 under
`irm … | iex` runs with no arguments at all: on Windows the environment is the
only way in (`$env:SYNAPSE_NO_START='1'` before the `irm` line). A flag given
on the command line wins over its twin.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapse-install", description="Install or upgrade Synapse")
    parser.add_argument("command", nargs="?", default="install", choices=["install", "upgrade"])
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Ask nothing: skip the AI question, keep every default")
    parser.add_argument("--no-start", action="store_true", help="Install, but do not start Synapse")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser at the end")
    parser.add_argument("--version", default="", help="Install this version instead of the latest")
    parser.add_argument("--with", dest="extras", action="append", default=[], metavar="PART",
                        help="Also install an optional part, kept on every upgrade: knowledge, telemetry")
    parser.add_argument("--method", default="uv", choices=["uv", "npm"], help=argparse.SUPPRESS)
    parser.add_argument("--uv", default="", help=argparse.SUPPRESS)
    parser.add_argument("--legacy-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--carrier", default="", help=argparse.SUPPRESS)
    parser.add_argument("--home", default="", help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.wait_pid:
        # `synapse upgrade` on Windows: the process that started us runs from
        # the files this upgrade replaces, and Windows will not replace them
        # while it lives.
        import time

        from .home import _alive

        deadline = time.monotonic() + 120
        while _alive(args.wait_pid) and time.monotonic() < deadline:
            time.sleep(0.5)

    from .flow import EXTRAS, Installer, Options

    extras = _parts(args.extras or [os.environ.get("SYNAPSE_WITH", "")])
    unknown = [e for e in extras if e not in EXTRAS]
    if unknown:
        parser.error(f"--with takes {', '.join(EXTRAS)} (not {', '.join(unknown)})")
    options = Options(
        yes=args.yes or _on("SYNAPSE_YES"),
        no_start=args.no_start or _on("SYNAPSE_NO_START"),
        no_browser=args.no_browser or _on("SYNAPSE_NO_BROWSER"),
        version=args.version or os.environ.get("SYNAPSE_INSTALL_VERSION", "").strip(),
        extras=tuple(extras),
        method=args.method, uv=args.uv, legacy_root=args.legacy_root, carrier=args.carrier,
        upgrade=args.command == "upgrade",
        home=Path(args.home).expanduser() if args.home else None,
    )
    return Installer(options).run()


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0")


def _parts(values: list[str]) -> list[str]:
    """`--with knowledge --with telemetry`, or `knowledge,telemetry` in one."""
    parts: list[str] = []
    for value in values:
        for part in (value or "").split(","):
            part = part.strip().lower()
            if part and part not in parts:
                parts.append(part)
    return parts


if __name__ == "__main__":
    sys.exit(main())
