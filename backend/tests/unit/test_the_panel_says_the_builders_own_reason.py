"""The Synapse AI panel says the builder's own reason for refusing a message, never "could not be reached" (F1666).

A message the builder refuses with a reason (a 4xx whose body says why, such as an image sent to a builder model that
cannot see images: "cli.claude.claude-sonnet-4-6 cannot see images. Pick a different builder model, or send the message
without them.") was said in the panel as "The builder could not be reached (422).", which reads as the server being
down and drops what to do. Both of the panel's sends, a new message and an answer to a question, threw that for any
response that was not ok.

Now `components/app/builderRefusal.ts` reads the response: a 4xx whose body says why in words (the backend's `detail`,
also as this edition's proxy wraps it, `{"error": "Backend Error 422: {\\"detail\\": …}"}`) is said in those words;
anything else is "The builder could not be reached (<status>)." as before.

Near misses: a 5xx is not reached even with a detail; a 422 whose detail is the request's field list (not words) is not
reached; a body that is not JSON, and an empty detail, are not reached.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
REFUSAL = SRC / "components/app/builderRefusal.ts"
CHATS = SRC / "components/app/builderChats.ts"
SAID = ("cli.claude.claude-sonnet-4-6 cannot see images. Pick a different builder model, or send the message without "
        "them.")


@pytest.fixture(scope="module")
def node():
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the panel's reading is TypeScript")

    def said(status: int, body: str) -> str:
        program = (f"import {{ builderRefusal }} from {json.dumps(REFUSAL.as_uri())};\n"
                   f"const r = new Response({json.dumps(body)}, {{ status: {status} }});\n"
                   "console.log(JSON.stringify(await builderRefusal(r)));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                               program], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-2000:]
        return json.loads(done.stdout)
    return said


def _proxied(status: int, backend_body: str) -> str:
    """The body this edition's proxy (`app/api/builder/*/route.ts`) answers with for a backend error."""
    return json.dumps({"error": f"Backend Error {status}: {backend_body}"})


class TestItsOwnWords:
    def test_a_refusal_through_the_proxy_says_its_reason(self, node):
        assert node(422, _proxied(422, json.dumps({"detail": SAID}))) == SAID

    def test_a_refusal_with_its_detail_says_it(self, node):
        assert node(400, json.dumps({"detail": SAID})) == SAID

    def test_both_sends_say_it(self):
        source = CHATS.read_text(encoding="utf-8")
        assert "await builderRefusal(response)" in source, "the send's error is the response's own reading"
        assert "The builder could not be reached" not in source, "said in one place, the helper"


class TestNearMisses:
    @pytest.mark.parametrize("status, body", [
        (500, _proxied(500, json.dumps({"detail": "Internal Server Error"}))),
        (502, json.dumps({"detail": SAID})),
        (422, _proxied(422, json.dumps({"detail": [{"loc": ["body", "message"], "msg": "Field required"}]}))),
        (404, "Not Found"),
        (400, _proxied(400, "Bad Request")),
        (400, json.dumps({"detail": "  "})),
    ])
    def test_anything_else_could_not_be_reached(self, node, status, body):
        assert node(status, body) == f"The builder could not be reached ({status})."
