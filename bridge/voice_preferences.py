"""Single self-hosted account's durable native speech preferences.

Catalogs belong to the owner TTS configuration. Device requests can only choose
those IDs; they never supply provider voices, credentials or preview text.
"""
from pathlib import Path
import os

from aiohttp import web

from .speech import SpeechConfig
from .tts import TTSConfig

VOICE_PREFERENCES = web.AppKey("duet_voice_preferences", object)
FIELDS = frozenset({"voice_preference", "voice_speed", "voice_language_code", "dictation_language_code"})


class VoicePreferences(SpeechConfig):
    FIELDS = FIELDS

    def __init__(self, data_dir, tts):
        self.tts = tts
        # Reuse the same bounded regular-file reader and atomic private-file
        # writer as speech/TTS; keep preferences separate from provider secrets.
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "voice-preferences.json"
        if self.path.exists():
            self.load()
            os.chmod(self.path, 0o600)

    @staticmethod
    def defaults():
        return {}

    def validate(self, updates, previous=None):
        if not isinstance(updates, dict) or set(updates) - FIELDS:
            raise ValueError("Unknown voice preference")
        result = dict(previous or {})
        result.update(updates)
        for key, value in result.items():
            if key == "voice_speed":
                if type(value) not in (int, float) or value not in (.8, 1, 1.2, 1.5):
                    raise ValueError("Invalid voice speed")
            elif not isinstance(value, str) or not 1 <= len(value) <= 64:
                raise ValueError("Invalid voice preference")
        return result

    @staticmethod
    def public(value):
        return dict(value)

    def update(self, delta):
        if not delta:
            raise ValueError("Empty voice settings")
        self.validate(delta)
        catalog = TTSConfig.catalog(self.tts.config.load())
        voices = {entry["id"] for language in catalog for entry in language["voices"]}
        codes = {language["code"] for language in catalog}
        for key, value in delta.items():
            allowed = voices if key == "voice_preference" else codes
            if key == "dictation_language_code":
                allowed = codes | {"auto"}
            if key != "voice_speed" and value not in allowed:
                raise ValueError("Unknown catalog selection")
        return self.save(delta)

    def projection(self):
        try:
            settings = self.tts.config.load()
        except (ValueError, OSError):
            settings = TTSConfig.defaults()
        catalog = TTSConfig.catalog(settings)
        defaults = {"voice_speed": 1.0, "dictation_language_code": "auto"}
        language = next((item for item in catalog if item["code"] == settings["default_language_code"]), None)
        if language:
            defaults.update(voice_language_code=language["code"], voice_preference=language["voices"][0]["id"])
        return {**defaults, **self.load()}

    def features(self, origin):
        try:
            settings = self.tts.config.load()
        except (ValueError, OSError):
            settings = TTSConfig.defaults()
        ready = self.tts.refresh_capability()
        revision = TTSConfig.revision(settings)
        languages = [{"code": language["code"], "display_name": language["display_name"],
            "voices": [{"id": voice["id"], "display_name": voice["display_name"],
                        "preview_url": f"{origin}/duet/voice-previews/{revision}/{voice['id']}.wav"}
                       for voice in language["voices"]]} for language in TTSConfig.catalog(settings)] if ready else []
        return {"cai_gleaming_goblet": {"defaultValue": bool(languages)},
                "mobile_deedee_config": {"defaultValue": {"speech_input": {
                    "default_language_code": settings["default_language_code"],
                    "supported_languages": languages,
                    "is_voice_multilingual_enabled": len(languages) > 1,
                    "is_language_beta": False}}}}


def register_voice_preferences(app, data_dir, tts):
    service = VoicePreferences(data_dir, tts)
    app[VOICE_PREFERENCES] = service
    return service
