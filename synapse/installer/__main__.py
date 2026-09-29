"""`python synapse-install.pyz [install|upgrade] [--yes] …` — see flow.py."""
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

    from .flow import Installer, Options

    yes = args.yes or os.environ.get("SYNAPSE_YES", "") not in ("", "0")
    options = Options(
        yes=yes, no_start=args.no_start, no_browser=args.no_browser, version=args.version,
        method=args.method, uv=args.uv, legacy_root=args.legacy_root, carrier=args.carrier,
        upgrade=args.command == "upgrade",
        home=Path(args.home).expanduser() if args.home else None,
    )
    return Installer(options).run()


if __name__ == "__main__":
    sys.exit(main())
