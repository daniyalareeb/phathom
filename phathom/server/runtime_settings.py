"""Runtime-tunable settings with .env defaults (EXTENSION_SPEC §6.5 PATCH /settings).

.env values are the defaults; PATCH /settings stores overrides in
``data/server_settings.json``. The Groq key is write-only (see api.py).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from phathom.config import settings

log = logging.getLogger("phathom.server.settings")

OVERRIDES_PATH = Path("data/server_settings.json")

# override key -> attribute on the global pydantic settings
MUTABLE = {
    "answer_delay_s": "ANSWER_DELAY_S",
    "max_call_s": "MAX_CALL_S",
    "silence_hangup_s": "SILENCE_HANGUP_S",
    "disclosure": "DISCLOSURE",
    "live_transcript": "LIVE_TRANSCRIPT",
    "side_panel_auto": "SIDE_PANEL_AUTO",
    "tts_engine": "TTS_ENGINE",
    "tts_voice_en": "TTS_VOICE_EN",
    "tts_voice_ur": "TTS_VOICE_UR",
    "keep_audio_days": "KEEP_AUDIO_DAYS",
    "summary_max_tokens": "SUMMARY_MAX_TOKENS",
    "owner_call_max_s": "OWNER_CALL_MAX_S",
    "caller_source": "CALLER_SOURCE",
    "talk_mode_enabled": "TALK_MODE_ENABLED",
    "test_tone_heard": "TEST_TONE_HEARD",
}


def _load() -> dict:
    try:
        return json.loads(OVERRIDES_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save(overrides: dict) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2))


def get(key: str):
    overrides = _load()
    if key in overrides:
        return overrides[key]
    attr = MUTABLE.get(key)
    if attr is None:
        raise KeyError(key)
    return getattr(settings, attr)


def all_effective() -> dict:
    return {k: get(k) for k in MUTABLE}


def set_many(patch: dict) -> dict:
    overrides = _load()
    for k, v in patch.items():
        if k not in MUTABLE:
            raise KeyError(k)
        if k == "caller_source" and v not in ("auto", "webrtc", "speaker_tap", "tab_capture"):
            raise KeyError(f"caller_source must be auto|webrtc|speaker_tap|tab_capture (got {v!r})")
        default = getattr(settings, MUTABLE[k])
        # Store only real overrides; resetting to the default clears the entry.
        if v == default:
            overrides.pop(k, None)
        else:
            overrides[k] = v
    _save(overrides)
    return all_effective()
