"""The languages a number answers in (V19, VOX1d): the one list both products check and the Phone tab reads."""
from __future__ import annotations

import ast
import pathlib

import pytest

from core.messaging.voice import languages
from core.messaging.voice.languages import (DEEPGRAM, GOOGLE, LANGUAGES, check, default_for_number, is_allowed,
                                            public, say_language, stt_for)


class TestTheList:
    def test_twilios_table_and_nothing_else(self):
        codes = [lang.code for lang in LANGUAGES]
        assert len(codes) == len(set(codes)) == 36
        assert {"en-US", "en-GB", "en-IN", "en-AU", "hi-IN", "ta-IN", "ml-IN"} <= set(codes)
        assert not is_allowed("multi")  # auto-detect stays off (V19)
        assert not is_allowed("en") and not is_allowed("") and not is_allowed(None)

    def test_every_default_is_a_pair_its_row_allows(self):
        for lang in LANGUAGES:
            provider, model = lang.default_stt
            assert model in lang.stt[provider], lang.code
            assert lang.tts and set(lang.tts) <= set(languages.TTS_PROVIDERS), lang.code

    def test_who_hears_and_speaks_the_edges(self):
        # Neither Nova model has Malayalam; ElevenLabs' Flash v2.5 has no Kannada, Malayalam, Marathi, Telugu, Thai.
        assert languages.get("ml-IN").stt == {GOOGLE: ("long",)}
        assert languages.get("ml-IN").default_stt == (GOOGLE, "long")
        for code in ("kn-IN", "ml-IN", "mr-IN", "te-IN", "th-TH"):
            assert languages.get(code).tts == (GOOGLE,), code
        # Flux is English only.
        assert [lang.code for lang in LANGUAGES if "flux" in lang.stt.get(DEEPGRAM, ())] == [
            "en-AU", "en-GB", "en-IN", "en-US"]

    def test_it_imports_nothing_from_the_engine(self):
        # Cloud's api imports it; the engine's modules must not come with it.
        tree = ast.parse(pathlib.Path(languages.__file__).read_text())
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert imported <= {"__future__", "logging", "dataclasses"}

    def test_the_pickers_shape(self):
        out = public()
        en_in = next(row for row in out["languages"] if row["code"] == "en-IN")
        assert en_in["name"] == "English (India)"
        assert en_in["default_stt"] == {"provider": DEEPGRAM, "model": "nova-3-general"}
        assert set(en_in["stt"]) == {DEEPGRAM, GOOGLE}
        assert out["default_language"] == "en-US"
        assert out["speech_models"][DEEPGRAM][0] == "nova-3-general"


class TestCheck:
    def test_an_empty_block_and_a_number_saved_before_vox1d_are_fine(self):
        assert check(None) == check({}) == []
        assert check({"language": "en-US", "voice": "", "tts_provider": "", "notice": "Hi"}) == []

    def test_an_unknown_language(self):
        (error,) = check({"language": "en-XX"})
        assert "en-XX" in error and "Choose a language" in error

    @pytest.mark.parametrize("voice, words", [
        ({"language": "kn-IN", "tts_provider": "ElevenLabs"}, "ElevenLabs has no voice for Kannada"),
        ({"tts_provider": "Azure"}, "voice provider is one of"),
        ({"language": "ml-IN", "transcription_provider": "Deepgram"}, "Deepgram can't hear Malayalam"),
        ({"language": "de-DE", "speech_model": "flux"}, "Deepgram's flux can't hear German"),
        ({"language": "en-IN", "transcription_provider": "Whisper"}, "speech recognition provider is one of"),
        ({"speech_model": "nova-9"}, "not a speech model"),
        ({"language": "en-US", "transcription_provider": "Google", "speech_model": "nova-3-general"},
         "nova-3-general is Deepgram's model, not Google's"),
    ])
    def test_what_cannot_serve_the_language(self, voice, words):
        errors = check(voice)
        assert errors and words in " ".join(errors), errors

    def test_what_can(self):
        assert check({"language": "en-IN", "transcription_provider": "Google", "speech_model": "long"}) == []
        assert check({"language": "en-GB", "speech_model": "flux"}) == []  # the model names its provider
        assert check({"language": "ml-IN", "tts_provider": "Google", "transcription_provider": "Google"}) == []


class TestWhatACallHearsWith:
    def test_the_languages_default_when_the_number_chose_nothing(self):
        assert stt_for({}) == (DEEPGRAM, "nova-3-general")
        assert stt_for({"language": "ml-IN"}) == (GOOGLE, "long")

    def test_the_numbers_own_choice(self):
        assert stt_for({"language": "en-US", "transcription_provider": "Google", "speech_model": "telephony"}) == (
            GOOGLE, "telephony")
        # A provider alone gets its model for the language: Google's first where Google isn't the default.
        assert stt_for({"language": "en-IN", "transcription_provider": "Google"}) == (GOOGLE, "long")
        assert stt_for({"language": "en-US", "speech_model": "flux"}) == (DEEPGRAM, "flux")

    def test_a_stored_choice_the_list_no_longer_allows_falls_back(self, caplog):
        # A retired model or a hand-edited document never reaches Twilio as markup it would refuse.
        assert stt_for({"language": "de-DE", "speech_model": "flux"}) == (DEEPGRAM, "nova-3-general")
        assert stt_for({"language": "ml-IN", "transcription_provider": "Deepgram"}) == (GOOGLE, "long")
        assert stt_for({"language": "en-US", "speech_model": "nova-1"}) == (DEEPGRAM, "nova-3-general")
        assert "can't hear" in caplog.text

    def test_an_unknown_language_is_left_to_twilio(self):
        assert stt_for({"language": "xx-XX"}) == ("", "")


class TestDefaults:
    @pytest.mark.parametrize("number, language", [
        ("+447700900123", "en-GB"), ("+61491570006", "en-AU"), ("+918045678901", "en-IN"),
        ("+14155550100", "en-US"), ("+4930123456", "en-US"), ("", "en-US"), (None, "en-US"),
        ("client:vox1b-browser", "en-US"),
    ])
    def test_a_numbers_language_follows_its_country_in_english(self, number, language):
        assert default_for_number(number) == language

    def test_say_speaks_english_sentences_in_an_english_voice(self):
        assert say_language("en-IN") == "en-IN"
        assert say_language("ta-IN") == "en-US"
        assert say_language("") == say_language(None) == say_language("en-XX") == "en-US"
