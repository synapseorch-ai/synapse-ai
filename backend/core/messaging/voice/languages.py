"""The languages a number can answer in, and who speaks and hears each (V19, VOX1d).

One list for both products: OSS's channel save and cloud's ``VoiceSettings`` check a number's ``voice`` block
against it, and the Phone tab's picker reads ``public()``. It imports nothing from the engine, so cloud's api can
import it.

Where the rows come from (read 2026-10-11, ``MEMORY/drafts/vox1d/sources.md`` in pro):

* **The codes** are Twilio's ConversationRelay table (36). ``multi`` (auto-detect) is left out: it needs Deepgram
  and ElevenLabs together and V19 keeps it off.
* **Who speaks it** (``tts``): Google serves every code. ElevenLabs' Flash v2.5, Twilio's default, has no Kannada,
  Malayalam, Marathi, Telugu or Thai, which is why Twilio's table gives those five Google. Amazon Polly serves 25.
* **Who hears it** (``stt``): Deepgram's Nova-3 serves every code but Malayalam; Nova-2 also lacks Kannada,
  Marathi, Tamil and Telugu. Flux is English only. Google's ``long`` serves every code, ``telephony`` the codes
  Twilio's table gives it.
* **Who hears the caller is Twilio's default unless the number chooses** (Naveen, VOX1d). Twilio's default depends on
  the customer's account: Deepgram for accounts that started ConversationRelay after 2025-09-12, Google before. A
  number that wants one way for certain picks a provider (and a model).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("voice")

GOOGLE, AMAZON, ELEVENLABS, DEEPGRAM = "Google", "Amazon", "ElevenLabs", "Deepgram"
TTS_PROVIDERS: tuple[str, ...] = (ELEVENLABS, GOOGLE, AMAZON)
TRANSCRIPTION_PROVIDERS: tuple[str, ...] = (DEEPGRAM, GOOGLE)
SPEECH_MODELS: dict[str, tuple[str, ...]] = {
    DEEPGRAM: ("nova-3-general", "nova-2-general", "flux"),
    GOOGLE: ("long", "telephony"),
}
DEFAULT_LANGUAGE = "en-US"


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    #: The text-to-speech providers that speak it.
    tts: tuple[str, ...]
    #: The speech-to-text providers that hear it, each with its models.
    stt: dict[str, tuple[str, ...]]


_ALL_TTS = (ELEVENLABS, GOOGLE, AMAZON)
_NO_POLLY = (ELEVENLABS, GOOGLE)
_GOOGLE_TTS = (GOOGLE,)
_NOVA = ("nova-3-general", "nova-2-general")
_NOVA_3 = ("nova-3-general",)
_ENGLISH = ("nova-3-general", "nova-2-general", "flux")
_LONG = ("long",)
_PHONE = ("long", "telephony")


def _row(code: str, name: str, tts: tuple[str, ...], deepgram: tuple[str, ...], google: tuple[str, ...]) -> Language:
    return Language(code, name, tts, {p: m for p, m in ((DEEPGRAM, deepgram), (GOOGLE, google)) if m})


LANGUAGES: tuple[Language, ...] = (
    _row("bg-BG", "Bulgarian", _NO_POLLY, _NOVA, _LONG),
    _row("cs-CZ", "Czech", _ALL_TTS, _NOVA, _LONG),
    _row("da-DK", "Danish", _ALL_TTS, _NOVA, _LONG),
    _row("de-DE", "German", _ALL_TTS, _NOVA, _PHONE),
    _row("en-AU", "English (Australia)", _ALL_TTS, _ENGLISH, _PHONE),
    _row("en-GB", "English (United Kingdom)", _ALL_TTS, _ENGLISH, _PHONE),
    _row("en-IN", "English (India)", _ALL_TTS, _ENGLISH, _LONG),
    _row("en-US", "English (United States)", _ALL_TTS, _ENGLISH, _PHONE),
    _row("es-ES", "Spanish (Spain)", _ALL_TTS, _NOVA, _PHONE),
    _row("es-US", "Spanish (United States)", _ALL_TTS, _NOVA, _PHONE),
    _row("fi-FI", "Finnish", _ALL_TTS, _NOVA, _LONG),
    _row("fr-CA", "French (Canada)", _ALL_TTS, _NOVA, _PHONE),
    _row("fr-FR", "French (France)", _ALL_TTS, _NOVA, _PHONE),
    _row("hi-IN", "Hindi", _ALL_TTS, _NOVA, _LONG),
    _row("hu-HU", "Hungarian", _NO_POLLY, _NOVA, _LONG),
    _row("id-ID", "Indonesian", _NO_POLLY, _NOVA, _LONG),
    _row("it-IT", "Italian", _ALL_TTS, _NOVA, _PHONE),
    _row("ja-JP", "Japanese", _ALL_TTS, _NOVA, _PHONE),
    _row("kn-IN", "Kannada", _GOOGLE_TTS, _NOVA_3, _LONG),
    _row("ko-KR", "Korean", _ALL_TTS, _NOVA, _PHONE),
    _row("ml-IN", "Malayalam", _GOOGLE_TTS, (), _LONG),
    _row("mr-IN", "Marathi", _GOOGLE_TTS, _NOVA_3, _LONG),
    _row("nl-BE", "Dutch (Belgium)", _ALL_TTS, _NOVA, _PHONE),
    _row("nl-NL", "Dutch (Netherlands)", _ALL_TTS, _NOVA, _PHONE),
    _row("pl-PL", "Polish", _ALL_TTS, _NOVA, _LONG),
    _row("pt-BR", "Portuguese (Brazil)", _ALL_TTS, _NOVA, _PHONE),
    _row("pt-PT", "Portuguese (Portugal)", _ALL_TTS, _NOVA, _PHONE),
    _row("ro-RO", "Romanian", _ALL_TTS, _NOVA, _LONG),
    _row("ru-RU", "Russian", _ALL_TTS, _NOVA, _LONG),
    _row("sv-SE", "Swedish", _ALL_TTS, _NOVA, _LONG),
    _row("ta-IN", "Tamil", _NO_POLLY, _NOVA_3, _LONG),
    _row("te-IN", "Telugu", _GOOGLE_TTS, _NOVA_3, _LONG),
    _row("th-TH", "Thai", _GOOGLE_TTS, _NOVA, _LONG),
    _row("tr-TR", "Turkish", _ALL_TTS, _NOVA, _LONG),
    _row("uk-UA", "Ukrainian", _NO_POLLY, _NOVA, _LONG),
    _row("vi-VN", "Vietnamese", _NO_POLLY, _NOVA, _LONG),
)

_BY_CODE = {lang.code: lang for lang in LANGUAGES}

#: A new number's language follows its country, English variants only: the notice, the filler and the goodbye are
#: English sentences, and the agent's own prompt decides what it speaks (Naveen, VOX1d).
_BY_COUNTRY_CODE = (("+44", "en-GB"), ("+61", "en-AU"), ("+91", "en-IN"))


def get(code: str | None) -> Language | None:
    return _BY_CODE.get(str(code or ""))


def is_allowed(code: str | None) -> bool:
    return get(code) is not None


def default_for_number(e164: str | None) -> str:
    """The language a number starts with: its country's English, else US English (a browser call has none)."""
    number = str(e164 or "")
    return next((lang for prefix, lang in _BY_COUNTRY_CODE if number.startswith(prefix)), DEFAULT_LANGUAGE)


def say_language(code: str | None) -> str:
    """The language for ``<Say>``: its sentences are English, so an English variant (the number's, if it is one)."""
    return code if code and code.startswith("en-") and is_allowed(code) else DEFAULT_LANGUAGE


def _provider_of(model: str) -> str:
    return next((p for p, models in SPEECH_MODELS.items() if model in models), "")


def check(voice: dict | None) -> list[str]:
    """What is wrong with a ``voice`` block's language and providers, as sentences a person can act on.

    An empty or missing field is never wrong: it means the default (a number saved before VOX1d has none).
    """
    if voice is not None and not isinstance(voice, dict):
        return ["The voice settings are an object."]
    voice = voice or {}
    errors: list[str] = []
    code = str(voice.get("language") or "")
    lang = get(code or DEFAULT_LANGUAGE)
    if lang is None:
        return [f"Calls can't be answered in “{code[:20]}”. Choose a language from the list."]
    tts = str(voice.get("tts_provider") or "")
    if tts and tts not in TTS_PROVIDERS:
        errors.append(f"The voice provider is one of {', '.join(TTS_PROVIDERS)}.")
    elif tts and tts not in lang.tts:
        errors.append(f"{tts} has no voice for {lang.name}. Choose {' or '.join(lang.tts)}.")
    provider = str(voice.get("transcription_provider") or "")
    model = str(voice.get("speech_model") or "")
    if provider and provider not in TRANSCRIPTION_PROVIDERS:
        errors.append(f"The speech recognition provider is one of {', '.join(TRANSCRIPTION_PROVIDERS)}.")
        return errors
    if model and not _provider_of(model):
        errors.append(f"“{model[:40]}” is not a speech model calls can use.")
        return errors
    if provider and model and _provider_of(model) != provider:
        errors.append(f"{model} is {_provider_of(model)}'s model, not {provider}'s.")
        return errors
    provider = provider or _provider_of(model)
    if provider and provider not in lang.stt:
        errors.append(f"{provider} can't hear {lang.name}. Choose {' or '.join(lang.stt)}.")
    elif model and model not in lang.stt.get(provider, ()):
        errors.append(f"{provider}'s {model} can't hear {lang.name}. "
                      f"Choose {' or '.join(lang.stt.get(provider, ()))}.")
    return errors


def stt_for(voice: dict | None) -> tuple[str, str]:
    """The (provider, model) a call hears with: the number's own choice if it can serve the language, else nothing,
    and Twilio's default hears it. A stored choice the list no longer allows (Twilio retired a model, a hand-edited
    document) falls back to Twilio's default rather than giving Twilio markup it would refuse.
    """
    voice = voice if isinstance(voice, dict) else {}
    model = str(voice.get("speech_model") or "")
    provider = str(voice.get("transcription_provider") or "") or _provider_of(model)
    if not provider:
        return "", ""
    lang = get(voice.get("language") or DEFAULT_LANGUAGE)
    models = lang.stt.get(provider, ()) if lang else ()
    if models and (not model or model in models):
        return provider, model or models[0]
    log.warning("a number's speech recognition (%s %s) can't hear %s; Twilio's default hears it", provider[:20],
                model[:40], str(voice.get("language") or DEFAULT_LANGUAGE)[:20])
    return "", ""


def public() -> dict:
    """The list as the Phone tab's picker reads it (VOX1c's options route)."""
    return {
        "languages": [{"code": lang.code, "name": lang.name, "tts": list(lang.tts),
                       "stt": {p: list(m) for p, m in lang.stt.items()},
                       # Twilio's own default hears a number that chooses nothing (it depends on the account).
                       "default_stt": None}
                      for lang in LANGUAGES],
        "default_language": DEFAULT_LANGUAGE,
        "tts_providers": list(TTS_PROVIDERS),
        "transcription_providers": list(TRANSCRIPTION_PROVIDERS),
        "speech_models": {p: list(m) for p, m in SPEECH_MODELS.items()},
    }
