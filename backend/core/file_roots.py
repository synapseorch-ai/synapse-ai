"""Where an agent's file tools may read — one definition (S5).

The Filesystem MCP server has always been started on a list of roots: the
linked repositories, the vault, and the directories named in Settings →
Allowed Directories. The engine's own file tools — `file_reader`'s
`read_file_by_lines` / `grep_file`, and `code_search`'s `grep`, `glob` and
`read_file_by_lines` — opened whatever path they were handed. An agent steered
by what it had just read could read `.env`, secrets in the data directory, or
`~/.aws/credentials`.

So there is one list now, and every file tool checks against it: the same list
the Filesystem server is given, built here and nowhere else.

`within()` is the containment test. It compares real paths (symlinks resolved,
`..` collapsed) and requires the separator after the base, so `vault-old` is
not inside `vault` (S8).

(1.9.x port: repositories come from `DATA_DIR/repos.json` and the vault is
`DATA_DIR/vault`, as everywhere else in this line.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REFUSAL_HINT = (
    "Agents may read only inside linked repositories, the vault, and the folders "
    "listed in Settings → Allowed Directories."
)


def repo_paths() -> list[str]:
    """The linked repositories that exist on this machine, in file order."""
    from core.config import DATA_DIR

    repos_file = Path(DATA_DIR) / "repos.json"
    if not repos_file.exists():
        return []
    try:
        repos = json.loads(repos_file.read_text(encoding="utf-8"))
        return [r["path"] for r in repos if r.get("path") and os.path.isdir(r["path"])]
    except Exception as e:  # noqa: BLE001
        print(f"Warning: Could not load repo paths: {e}")
        return []


def allowed_roots() -> list[str]:
    """Repositories, then the vault, then Allowed Directories not already covered.

    The order and the de-duplication are the Filesystem server's argument list,
    exactly — `core/server.py` starts it from this.
    """
    from core.config import DATA_DIR, load_settings

    roots = repo_paths() + [str(Path(DATA_DIR) / "vault")]
    seen = {os.path.realpath(p) for p in roots}
    for d in load_settings().get("bash_allowed_dirs", []) or []:
        if not d or not os.path.isdir(d):
            continue
        real = os.path.realpath(d)
        if real not in seen:
            roots.append(d)
            seen.add(real)
    return roots


def within(path: str | os.PathLike, roots: list[str] | list[Path]) -> bool:
    """True if `path`, fully resolved, is one of `roots` or inside one."""
    resolved = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(root)
        if resolved == base or resolved.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def refusal(path: str) -> str:
    return f"Access denied: '{path}' is outside the folders agents may read. {REFUSAL_HINT}"
