"""A new conversation's id comes from one helper, and never from Math.random or the clock (F1471).

CodeQL (js/insecure-randomness, PR #379) found the chat page and the agent test panel falling back to
`Math.random()` where `crypto.randomUUID` is missing: Synapse opened over plain http on a LAN address. The builder's
chats fell back to `Date.now()`. An id is not what lets anyone into a conversation, but each now comes from
`frontend/src/lib/sessionId.ts`: `randomUUID`, else a UUID from `crypto.getRandomValues`. Its behaviour is held by the
web app's vitest (sessionId.test.ts); this holds every caller to it.
"""
from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
HELPER = FRONTEND / "lib" / "sessionId.ts"
CALLERS = [
    FRONTEND / "app" / "(app)" / "page.tsx",
    FRONTEND / "components" / "settings" / "AgentTest.tsx",
    FRONTEND / "components" / "app" / "builderChats.ts",
]


def _code(path: Path) -> str:
    """The file without its comments' lines, so a sentence about Math.random is not code using it."""
    return "\n".join(line for line in path.read_text("utf-8").splitlines()
                     if not line.lstrip().startswith(("//", "*", "/*")))


def test_the_helper_uses_the_platforms_random_only():
    code = _code(HELPER)
    assert "getRandomValues" in code and "randomUUID" in code
    assert "Math.random" not in code and "Date.now" not in code


def test_every_conversation_id_comes_from_the_helper():
    for path in CALLERS:
        code = _code(path)
        assert "import { newSessionId } from '@/lib/sessionId';" in code, path.name
        assert "Math.random" not in code and "String(Date.now())" not in code, path.name


def test_randomuuid_is_called_in_the_helper_alone():
    callers = sorted(str(p.relative_to(FRONTEND)) for p in FRONTEND.rglob("*")
                     if p.suffix in {".ts", ".tsx"} and "node_modules" not in p.parts
                     and ".randomUUID(" in _code(p) and p != HELPER)
    assert callers == []
