"""TTS: edge-tts primary, Groq TTS fallback for English — SPEC §9.7.

Status Oct 2026: Groq TTS is effectively unavailable (orpheus needs org-terms
acceptance in the Groq console; playai-tts is decommissioned). edge-tts is the
working path. The spec'd chain is kept: edge -> Groq (en) -> raise.
"""

import hashlib
import logging
from pathlib import Path

import edge_tts
import groq

from phathom.ai.groq_client import get_client, with_retry
from phathom.audio.devices import _run as run_cmd
from phathom.config import settings

log = logging.getLogger("phathom.ai.tts")

CACHE_DIR = Path("data/tts-cache")


def _cache_path(engine: str, voice: str, text: str) -> Path:
    digest = hashlib.sha1(f"{engine}|{voice}|{text}".encode()).hexdigest()
    return CACHE_DIR / f"{digest}.wav"


def _voice_for(lang: str) -> str:
    if lang == "ur":
        return settings.TTS_VOICE_UR
    return settings.TTS_VOICE_EN


async def _edge_wav(text: str, voice: str, dest: Path) -> None:
    tmp = dest.with_suffix(".mp3")
    await edge_tts.Communicate(text, voice).save(str(tmp))
    rc, _, err = await run_cmd(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(tmp), "-ar", "48000", "-ac", "1", str(dest))
    try:
        tmp.unlink()
    except OSError:
        pass
    if rc != 0:
        raise RuntimeError(f"ffmpeg tts convert failed: {err}")


async def _groq_wav(text: str, dest: Path) -> None:
    client = get_client()

    async def _call():
        # SPEC model; if the account lacks terms acceptance this raises — caller handles.
        resp = await client.audio.speech.create(
            model="canopylabs/orpheus-v1-english", voice=settings.GROQ_TTS_VOICE,
            input=text, response_format="wav")
        data = await resp.aread() if hasattr(resp, "aread") else bytes(resp)
        dest.write_bytes(data)

    await with_retry(_call, what="groq-tts")


async def synth(text: str, lang: str) -> Path:
    """Synthesize to a 48 kHz mono wav, cached by sha1(engine+voice+text)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("synth: empty text")
    engine = settings.TTS_ENGINE
    if engine == "groq" and lang == "ur":
        log.warning("TTS_ENGINE=groq does not support Urdu; using edge for this line")
        engine = "edge"
    if engine == "edge":
        voice = _voice_for(lang)
        dest = _cache_path("edge", voice, text)
        if dest.exists():
            return dest
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            await _edge_wav(text, voice, dest)
            return dest
        except Exception as e:
            log.warning("edge-tts failed (%r)", e)
            if lang == "ur":
                raise
            log.info("falling back to Groq TTS for English")
    else:
        voice = settings.GROQ_TTS_VOICE
        dest = _cache_path("groq", voice, text)
        if dest.exists():
            return dest
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await _groq_wav(text, dest)
        return dest
    except groq.RateLimitError:
        log.warning("Groq TTS 429; retrying once without extra wrapper")
        await _groq_wav(text, dest)
        return dest


async def presynth_fixed_lines() -> dict[tuple[str, str], Path]:
    """Pre-synthesise greeting/disclosure/fallback/goodbye in en+ur (daemon start)."""
    from phathom.ai.prompts import fixed_line
    out: dict[tuple[str, str], Path] = {}
    for key in ("greeting", "disclosure", "fallback", "goodbye"):
        for lang in ("en", "ur"):
            try:
                out[(key, lang)] = await synth(fixed_line(key, lang), lang)
            except Exception as e:
                log.warning("presynth %s/%s failed: %r", key, lang, e)
    return out
