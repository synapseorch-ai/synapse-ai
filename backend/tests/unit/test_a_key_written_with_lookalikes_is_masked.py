"""A credential is matched with its dash, space and invisible lookalikes folded, and masked as written (F1386, TRI1;
eb 11-05; Naveen 11-05: "Build it").

`secret_scrub.LOOKALIKES` is the one table: the builder's door folds a message through it, and a held value is matched
through it (F1339). The credential shapes are matched through it too, wherever they are read (`scrub_credentials`,
`holds_credential`, a curl's parts): a key written with any of them is masked whole, and the text around it is kept
exactly as it was written. Every key here is assembled at run time, each lookalike by its code point.
"""
from __future__ import annotations

import pytest

from core.secret_scrub import CREDENTIAL_MARK, LOOKALIKES, holds_credential, masked_output, scrub_credentials

#: The dashes the table folds to "-", by code point.
DASHES = [0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D,
          0x2043, 0xFE31, 0xFE32, 0x2E3A, 0x2E3B, 0x02D7]
SOFT_HYPHEN = 0x00AD
TAIL = "".join(chr(c) for c in range(ord("a"), ord("a") + 12)) + "0123456789AB"


def _cases(d: str) -> list[tuple[str, str]]:
    """(text, what it reads masked) for each shape, its hyphens written as `d`."""
    sk = f"sk{d}proj{d}{TAIL}"
    slack = f"xoxb{d}1234567890{d}abcdefGHIJ12"
    bearer = f"abcd1234{d}efgh5678{d}ijkl9012"
    label = f"abc123{d}def456{d}ghi789"
    header, value = f"X{d}Api{d}Key", f"ab12{d}cd34{d}ef56{d}gh78"
    return [
        (f"My key {sk} works.", f"My key {CREDENTIAL_MARK} works."),
        (f"token {slack} here", f"token {CREDENTIAL_MARK} here"),
        (f"Authorization: Bearer {bearer}", f"Authorization: Bearer {CREDENTIAL_MARK}"),
        (f"the API key is {label}.", f"the API key is {CREDENTIAL_MARK}."),
        (f"curl -H '{header}: {value}' https://api.example.com/v1",
         f"curl -H '{header}: {CREDENTIAL_MARK}' https://api.example.com/v1"),
    ]


class TestTheTable:
    @pytest.mark.parametrize("point", DASHES)
    def test_each_dash_folds_to_a_hyphen(self, point):
        assert LOOKALIKES[point] == "-"

    def test_a_soft_hyphen_is_dropped(self):
        assert LOOKALIKES[SOFT_HYPHEN] is None


class TestAKeyWithALookalikeIsMasked:
    @pytest.mark.parametrize("point", DASHES, ids=hex)
    def test_every_shape_with_every_dash(self, point):
        for text, masked in _cases(chr(point)):
            got, removed = scrub_credentials(text)
            assert got == masked and removed >= 1, (hex(point), text, got)
            assert holds_credential(text), (hex(point), text)

    def test_a_soft_hyphen_inside_a_key(self):
        key = f"sk-proj-abcd{chr(SOFT_HYPHEN)}{TAIL}"
        assert scrub_credentials(f"use {key} now") == (f"use {CREDENTIAL_MARK} now", 1)
        assert holds_credential(key)

    def test_a_zero_width_space_and_a_no_break_space(self):
        key = f"sk-proj-{chr(0x200B)}{TAIL}"
        assert scrub_credentials(f"Bearer{chr(0x202F)}{key}")[0] == f"Bearer{chr(0x202F)}{CREDENTIAL_MARK}"

    def test_the_text_around_it_is_kept_as_written(self):
        en, em = chr(0x2013), chr(0x2014)
        key = f"sk{chr(0x2011)}proj{chr(0x2011)}{TAIL}"
        text = f"Pages 3{en}5 {em} {key} {em} done{chr(0x00A0)}today"
        assert scrub_credentials(text)[0] == f"Pages 3{en}5 {em} {CREDENTIAL_MARK} {em} done{chr(0x00A0)}today"


class TestWhereItIsRead:
    KEY = f"sk{chr(0x2011)}proj{chr(0x2011)}{TAIL}"

    def test_a_python_tools_output(self):
        said = masked_output(f"Traceback: auth failed for {self.KEY}")
        assert said == f"Traceback: auth failed for {CREDENTIAL_MARK}"

    def test_a_providers_refusal(self):
        import httpx

        from core.llm_providers import _refusal_said

        response = httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {self.KEY}."}})
        said = _refusal_said(response)
        assert CREDENTIAL_MARK in said and "proj" not in said and TAIL not in said, said

    def test_a_clis_words(self):
        from core.llm_providers import _cli_said

        said = _cli_said("claude", f"invalid key {self.KEY}")
        assert CREDENTIAL_MARK in said and TAIL not in said, said

    def test_the_door_as_before(self):
        from core.native_builder.door import check

        assert CREDENTIAL_MARK in check(f"my key is {self.KEY}").text


class TestNoKeyNoChange:
    def test_prose_dashes_are_left_as_written(self):
        en, em, minus = chr(0x2013), chr(0x2014), chr(0x2212)
        text = f"Read pages 3{en}5 of the 2026{en}27 report {em} then reply. It was {minus}4 degrees."
        assert scrub_credentials(text) == (text, 0)
        assert not holds_credential(text)

    def test_a_key_with_plain_hyphens_as_before(self):
        key = f"sk-proj-{TAIL}"
        assert scrub_credentials(f"My key {key} works.") == (f"My key {CREDENTIAL_MARK} works.", 1)
