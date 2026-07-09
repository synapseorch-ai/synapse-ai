"""Unit tests for CLI provider auth-failure detection (_match_auth).

Guards against the issue #345 regression where a bare "auth" substring match
flagged normal model output ("authority", "authorization", ...) as an
authentication failure and aborted otherwise successful CLI runs.

`call_cli_provider` itself is subprocess/integration-only; only the pure
`_match_auth` helper is unit-tested here.
"""
import pytest

from core.llm_providers import _match_auth


class TestAuthDetection:
    # Real auth-failure strings emitted by CLIs — these MUST still be detected.
    @pytest.mark.parametrize("text", [
        "Invalid API key · Please run /login",
        "Not logged in",
        "Please sign in to continue",
        "401 Unauthorized",
        "Your session has expired. Please log in.",
        "Authentication required",
        "Error: authentication failed",
        "You are not authenticated. Run the CLI to log in.",
    ])
    def test_real_auth_failures_match(self, text):
        assert _match_auth(text) is True

    # Normal model output — these MUST NOT be flagged (the issue #345 regression).
    @pytest.mark.parametrize("text", [
        "The authority granted authorization to the authorized author.",
        "Use API key rotation for better security.",
        "Successfully authenticated the request earlier; here is your answer.",
        "The author writes about authorization models and authoritative sources.",
        (
            "Here is my analysis of the workflow:\n"
            "1. The transform step builds a work packet.\n"
            "2. Each reviewer has authority over its own branch.\n"
            "3. We then merge the authorized results and synthesize a summary."
        ),
    ])
    def test_normal_output_does_not_match(self, text):
        assert _match_auth(text) is False

    @pytest.mark.parametrize("text", ["", None])
    def test_empty_or_none_does_not_match(self, text):
        assert _match_auth(text) is False
