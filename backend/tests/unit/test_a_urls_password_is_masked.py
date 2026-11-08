"""A URL's password is masked wherever the shapes are, its user kept (F1343, TRI1; BLD1's find while building F1341;
Naveen 11-04: "Build as planned").

`scrub_credentials` knew keys by their shapes and their labels, and a URL's user part by neither, so a pasted
`https://ops:<password>@host:9411/x` reached the model with its password unless the password happened to be shaped like
a key or held. The password in any `scheme://user:password@host` is masked now: at the door, in a stored run error and
wherever else the shapes run. The user stays: it names the connection, never its secret. A password with "@" or ":" in
it is masked to the last "@" before the host. An address with no password, an email, and text with no scheme are left.

The password below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core.native_builder import door
from core.orchestration import run_error
from core.secret_scrub import CREDENTIAL_MARK, holds_credential, scrub_credentials

PW = "Tr0ub4dor" + "&3x"


class TestItsPasswordIsMasked:
    @pytest.mark.parametrize("url, kept", [
        (f"https://ops:{PW}@zipkin.example:9411/x", f"https://ops:{CREDENTIAL_MARK}@zipkin.example:9411/x"),
        (f"redis://:{PW}@cache.example:6379/0", f"redis://:{CREDENTIAL_MARK}@cache.example:6379/0"),
        (f"postgresql://app:{PW}@db.example/app?sslmode=require",
         f"postgresql://app:{CREDENTIAL_MARK}@db.example/app?sslmode=require"),
        (f"mongodb+srv://u:{PW}@cluster0.example/x", f"mongodb+srv://u:{CREDENTIAL_MARK}@cluster0.example/x"),
        ("https://ops:p@ss:w0rd" + "@host.example/x", f"https://ops:{CREDENTIAL_MARK}@host.example/x"),
    ])
    def test_in_any_scheme_and_its_user_kept(self, url, kept):
        assert scrub_credentials(f"connect {url} please") == (f"connect {kept} please", 1)
        assert holds_credential(url)

    def test_once_masked_it_stays_as_it_is(self):
        once, _ = scrub_credentials(f"https://ops:{PW}@zipkin.example:9411/x")
        assert scrub_credentials(once) == (once, 0)

    def test_at_the_door(self):
        checked = door.check(f"Zipkin is at https://ops:{PW}@zipkin.example:9411/x, read the traces")
        assert PW not in checked.text and checked.removed == 1 and checked.reason == "masked", checked
        assert "https://ops:" in checked.text and "@zipkin.example:9411/x" in checked.text

    def test_in_a_stored_run_error(self):
        said = run_error.clean(f"could not reach https://ops:{PW}@zipkin.example:9411/api/v2/traces: timed out")
        assert PW not in said and f"https://ops:{CREDENTIAL_MARK}@zipkin.example:9411" in said


class TestWhatHoldsNoPasswordIsLeft:
    @pytest.mark.parametrize("text", [
        "https://zipkin.example:9411/x",
        "https://ops@zipkin.example/x",
        "write to ops@example.com or mailto:ops@example.com",
        "the path https://host.example/a?b=c:d@e is odd",
        "ops:secretword@host with no scheme",
    ])
    def test_it_is_untouched(self, text):
        assert scrub_credentials(text) == (text, 0)
