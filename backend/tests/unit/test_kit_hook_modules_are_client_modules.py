"""A kit module that uses React's hooks says `'use client'` (ONB3, D326; F1060's follow-up).

Cloud copies these folders into its web (pro `scripts/sync-ui.sh`), where some pages are server components: cloud's
Home imports `components/connect`, which imports the `components/help` barrel. A module a server component reaches
that calls `useEffect` or `useSyncExternalStore` without the directive fails cloud's `next build` ("You're importing a
module that depends on `useEffect` into a React Server Component module"), and neither OSS's tsc, its lint nor its
own build sees it, since OSS's pages are client components. `tour/hold.ts` shipped that way and was caught by cloud's
build. So the rule is pinned here, where the kit is written.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"

#: The folders cloud's `scripts/sync-ui.sh` copies, as it lists them.
SYNCED = ("components/ui", "components/orchestration", "components/knowledge", "components/help", "components/run",
          "components/assistant", "components/providers", "components/phone", "components/boards", "components/team",
          "components/tabs", "components/connect", "components/starters")

HOOKS = re.compile(r"import\s*\{[^}]*\b(use[A-Z]\w*|createContext)\b[^}]*\}\s*from\s*['\"]react['\"]", re.S)
CLIENT = re.compile(r"\s*(//[^\n]*\n\s*)*['\"]use client['\"]")


def test_every_synced_folder_is_there():
    missing = [d for d in SYNCED if not (SRC / d).is_dir()]
    assert not missing, f"folders the pin reads are gone (update SYNCED with sync-ui.sh): {missing}"


def test_a_synced_module_with_hooks_is_a_client_module():
    bare = sorted(
        str(p.relative_to(SRC))
        for d in SYNCED
        for p in (SRC / d).rglob("*.ts*")
        if ".test." not in p.name and HOOKS.search(text := p.read_text("utf-8")) and not CLIENT.match(text)
    )
    assert not bare, f"modules that use React's hooks with no 'use client' first: {bare}"
