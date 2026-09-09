"""
What counts as an image the agent can be sent.

There was no answer anywhere. `ChatRequest.images` was a bare `list[str]`; the
"max 5" in its comment was enforced only by `[:5]` slices inside each
provider's content builder, so a caller reaching the API directly was bounded
by nothing. Nothing checked size, so a 20 MB PNG became ~27 MB of base64 in a
JSON body and, on the worker path, a Redis job argument. Nothing checked type:
the mime came from the data URI's own header and was handed to Anthropic as
`media_type`.
"""
import base64

import pytest
from pydantic import ValidationError

from core import images as img
from core.models import ChatRequest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


def uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


class TestTheBytesDecide:
    @pytest.mark.parametrize("raw,expected", [
        (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"),
        (b"GIF89a" + b"\x00" * 20, "image/gif"),
    ])
    def test_it_recognises_what_it_should(self, raw, expected):
        assert img.sniff(raw) == expected

    def test_renaming_a_file_does_not_change_what_is_in_it(self):
        """The cheap version of the mistake. The deliberate one is a header
        that says image/png in front of something else — same test."""
        with pytest.raises(img.ImageRejected, match="not a PNG"):
            img.validate([uri(b"this is just text", "image/png")])

    def test_a_format_no_provider_reads_is_refused_here(self):
        """Rather than at Bedrock, several seconds later, as somebody else's
        error message."""
        with pytest.raises(img.ImageRejected):
            img.validate([uri(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40, "image/heic")])


class TestTheLimits:
    def test_six_is_too_many(self):
        with pytest.raises(img.ImageRejected, match="Up to 5"):
            img.validate([uri(PNG)] * 6)

    def test_five_is_fine(self):
        assert len(img.validate([uri(PNG)] * 5)) == 5

    def test_one_oversized_image_is_named(self):
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(img.ImageRejected, match="the limit is 5 MB"):
            img.validate([uri(big)])

    def test_several_that_fit_alone_can_still_be_too_much_together(self):
        each = b"\x89PNG\r\n\x1a\n" + b"\x00" * (int(4.5 * 1024 * 1024))
        with pytest.raises(img.ImageRejected, match="together"):
            img.validate([uri(each)] * 5)


class TestMalformedInput:
    @pytest.mark.parametrize("bad", [
        "data:image/png;base64",          # no comma — this used to raise ValueError
        "",                               # empty
        "iVBORw0KGgo=",                   # bare base64, once assumed to be a PNG
        "https://example.com/cat.png",    # a URL nothing would have fetched
    ])
    def test_it_is_refused_with_a_reason(self, bad):
        with pytest.raises(img.ImageRejected):
            img.validate([bad])

    def test_base64_that_is_not_base64(self):
        with pytest.raises(img.ImageRejected, match="valid base64"):
            img.validate(["data:image/png;base64,!!!!not base64!!!!"])


class TestTheRequestEnforcesIt:
    def test_a_good_image_passes(self):
        assert len(ChatRequest(message="what is this", images=[uri(PNG)]).images) == 1

    def test_a_bad_one_never_reaches_the_engine(self):
        with pytest.raises(ValidationError) as exc:
            ChatRequest(message="hi", images=[uri(b"nope")])
        # The message is written to be shown to a person, so it must survive.
        assert "not a PNG" in str(exc.value)

    def test_no_images_is_the_ordinary_case(self):
        assert ChatRequest(message="hi").images == []


class TestWhoCanSee:
    @pytest.mark.parametrize("model", ["gpt-4o", "claude-sonnet-4-6", "gemini-2.5-flash",
                                       "bedrock.amazon.nova-pro-v1:0", "synapse.nova-pro"])
    def test_these_can(self, model):
        assert img.model_can_see_images(model)

    @pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner",
                                       "hf.llava", "cli.claude.sonnet-4-5", "cli.gemini.pro"])
    def test_these_drop_it_on_the_floor(self, model):
        """DeepSeek and HuggingFace warn and discard; the CLI providers discard
        in silence. Either way the person watched their attachment be ignored."""
        assert not img.model_can_see_images(model)


class TestAnImageOnItsOwnIsAMessage:
    def test_no_empty_text_block_is_sent_with_it(self):
        """Every provider here rejects an empty text block, which is what made
        the "[Images attached]" placeholder necessary in the first place."""
        from core.llm_providers import (_build_anthropic_image_content,
                                        _build_openai_image_content)
        for build in (_build_anthropic_image_content, _build_openai_image_content):
            parts = build("", [uri(PNG)])
            assert all(p.get("type") != "text" for p in parts), build.__name__

    def test_text_still_travels_when_there_is_some(self):
        from core.llm_providers import _build_openai_image_content
        parts = _build_openai_image_content("what is this", [uri(PNG)])
        assert any(p.get("type") == "text" and p["text"] == "what is this" for p in parts)
