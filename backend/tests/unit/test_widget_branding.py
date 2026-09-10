"""What a customer may change about how their widget looks, and what they may not.

Two of these matter more than the rest. `normalise_accent` is the only reason it
is safe to interpolate a customer-supplied string into a CSS custom property —
nothing but a hex colour gets through it. And `readable_on` is what stops a brand
colour the customer loves from producing text nobody can read on it.
"""
import pytest

from core.chat_widgets import branding


class TestTheAccentIsTheOnlyStringThatReachesCss:
    """It ends up in `style="--accent: …"`, so this is a boundary, not a tidy-up."""

    def test_shorthand_expands(self):
        assert branding.normalise_accent("#ABC") == "#aabbcc"

    def test_full_hex_lowercases(self):
        assert branding.normalise_accent("#4F46E5") == "#4f46e5"

    @pytest.mark.parametrize(
        "value",
        [
            "red",                          # a name, not a hex
            "rgb(1,2,3)",                   # a function
            "#12345",                       # wrong length
            "#zzzzzz",                      # not hex digits
            "#fff; background: url(x)",     # the one that matters
            "",
            None,
            123,
        ],
    )
    def test_anything_else_falls_back_rather_than_reaching_the_page(self, value):
        assert branding.normalise_accent(value) == branding.PRESET_ACCENTS[0]


class TestReadableOn:
    """A customer picks their brand colour, not a text colour. We pick that."""

    def test_a_dark_accent_takes_white_text(self):
        assert branding.readable_on("#18181b") == "#ffffff"
        assert branding.readable_on("#4f46e5") == "#ffffff"

    def test_a_pale_accent_takes_dark_text(self):
        """The case the naive average gets wrong and ships as white-on-yellow."""
        assert branding.readable_on("#facc15") == "#18181b"
        assert branding.readable_on("#ffffff") == "#18181b"

    def test_every_preset_is_legible(self):
        """A palette that shipped an unreadable swatch would be our mistake, not
        the customer's."""
        for accent in branding.PRESET_ACCENTS:
            assert branding.readable_on(accent) in ("#ffffff", "#18181b")

    def test_it_survives_junk_without_raising(self):
        assert branding.readable_on("not a colour") == "#ffffff"


class TestTheVocabularyIsClosed:
    @pytest.mark.parametrize(
        "field,default",
        [("font", "system"), ("theme", "auto"), ("header_style", "accent"),
         ("launcher", "bubble"), ("position", "right")],
    )
    def test_an_unknown_value_falls_back_to_the_default(self, field, default):
        assert branding.clean({field: "something-we-do-not-serve"})[field] == default

    def test_a_known_value_is_kept(self):
        cleaned = branding.clean(
            {"font": "serif", "theme": "dark", "header_style": "neutral", "launcher": "logo"}
        )
        assert cleaned["font"] == "serif"
        assert cleaned["theme"] == "dark"
        assert cleaned["header_style"] == "neutral"
        assert cleaned["launcher"] == "logo"

    def test_no_font_needs_a_download(self):
        """The reason the list is five names and not a Google Fonts picker: a
        webfont is a third-party request from the customer's visitors, which is a
        live GDPR problem in the EU, and one that loads late repaints a widget on
        a page that is not ours."""
        assert set(branding.FONTS) == {"system", "sans", "serif", "rounded", "mono"}

    def test_fields_it_does_not_own_are_left_alone(self):
        assert branding.clean({"agent_id": "agent_1", "accent": "#000"})["agent_id"] == "agent_1"


class TestOneShapeForTheWidgetAndItsPreview:
    """The preview renders the real widget from the same dict. If these ever
    diverge the preview starts lying, which is worse than not having one."""

    def test_it_carries_everything_the_page_renders(self):
        theme = branding.public_theme({"accent": "#facc15", "font": "serif", "logo": "data:x"})
        assert theme["accent"] == "#facc15"
        assert theme["on_accent"] == "#18181b"      # computed, not stored
        assert theme["font"] == "serif"
        assert theme["logo"] == "data:x"

    def test_it_never_carries_the_allow_list(self):
        """Configuration the owner reads, not something a visitor is told — it
        would answer 'which other sites run this widget'."""
        theme = branding.public_theme(
            {"accent": "#000", "allowed_origins": ["https://acme.com"], "public_key": "pk_x"}
        )
        assert "allowed_origins" not in theme
        assert "public_key" not in theme
