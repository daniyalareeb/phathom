"""Groq Whisper STT — SPEC §9.7."""

import io
import logging
import re
import wave
from pathlib import Path

import groq

from phathom.ai.groq_client import get_client, with_retry
from phathom.audio.devices import _run as run_cmd
from phathom.config import settings

log = logging.getLogger("phathom.ai.stt")

# Whisper invents these on silence/noise (lowercased, punctuation stripped).
HALLUCINATION_PHRASES = {"thank you", "thanks for watching", "you", "bye", ".", "شکریہ"}


def _clean(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"[^\w\s\u0600-\u06FF]", "", text).strip()


def is_hallucination(text: str, duration_s: float, recent: list[str] | None = None) -> bool:
    """Whisper hallucination filter: empty, known junk on short audio, or 3x repeat."""
    if not (text or "").strip():
        return True
    if duration_s < 1.0 and _clean(text) in HALLUCINATION_PHRASES:
        return True
    if recent:
        norm = _clean(text)
        tail = [_clean(t) for t in recent[-2:]]
        if norm and len(tail) == 2 and all(t == norm for t in tail):
            return True
    return False


def _pcm_to_wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def _create_transcription(**kwargs):
    client = get_client()
    try:
        return await with_retry(
            lambda: client.audio.transcriptions.create(**kwargs), what="stt"
        )
    except groq.RateLimitError:
        if kwargs.get("model") != settings.STT_MODEL_LIVE:
            log.warning("STT 429 on %s; falling back once to %s",
                        kwargs.get("model"), settings.STT_MODEL_LIVE)
            kwargs["model"] = settings.STT_MODEL_LIVE
            return await with_retry(
                lambda: client.audio.transcriptions.create(**kwargs), what="stt-fallback"
            )
        raise


async def transcribe_pcm(pcm: bytes, model: str, prompt: str | None = None) -> str:
    """Transcribe one utterance (16 kHz s16le mono). Language unset (auto: Urdu works)."""
    duration_s = len(pcm) / 2 / 16000
    wav = _pcm_to_wav_bytes(pcm)
    resp = await _create_transcription(
        model=model,
        file=("utterance.wav", wav),
        temperature=0,
        prompt=prompt or f"Phone call with {settings.OWNER_SHORT_NAME}. Urdu and English.",
    )
    text = (getattr(resp, "text", "") or "").strip()
    if is_hallucination(text, duration_s):
        log.info("STT filtered hallucination: %r (%.2fs)", text, duration_s)
        return ""
    return text


async def _split_big(path: Path, tmpdir: Path) -> list[tuple[Path, float]]:
    """Files over 20MB -> 10-minute 16 kHz mono flac chunks. Returns (path, offset_s)."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    # probe duration
    rc, out, _ = await run_cmd(
        "ffprobe", "-hide_banner", "-v", "error",
        "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    total = float(out.strip()) if rc == 0 and out.strip() else 0.0
    if total <= 0:
        return [(path, 0.0)]
    idx, offset = 0, 0.0
    while offset < total:
        chunk = tmpdir / f"chunk_{idx:03d}.flac"
        rc, _, err = await run_cmd(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(offset), "-t", "600", "-i", str(path),
            "-ac", "1", "-ar", "16000", str(chunk))
        if rc != 0 or not chunk.exists():
            raise RuntimeError(f"ffmpeg chunk failed at {offset}s: {err}")
        chunks.append((chunk, offset))
        offset += 600
        idx += 1
    return chunks


async def transcribe_file(path: Path, model: str) -> list[dict]:
    """Full-file transcription with word timestamps. Returns [{start, end, text}]."""
    path = Path(path)
    tmpdir = path.parent / "_chunks"
    chunks = [(path, 0.0)]
    if path.stat().st_size > 20 * 1024 * 1024:
        chunks = await _split_big(path, tmpdir)
    out: list[dict] = []
    try:
        for chunk_path, offset in chunks:
            with open(chunk_path, "rb") as f:
                data = f.read()
            resp = await _create_transcription(
                model=model,
                file=(chunk_path.name, data),
                response_format="verbose_json",
                temperature=0,
            )
            segs = getattr(resp, "segments", None) or []
            for s in segs:
                if isinstance(s, dict):
                    start, end, text = s.get("start", 0), s.get("end", 0), s.get("text", "")
                else:
                    start, end, text = getattr(s, "start", 0), getattr(s, "end", 0), getattr(s, "text", "")
                text = (text or "").strip()
                if text:
                    out.append({"start": float(start) + offset, "end": float(end) + offset, "text": text})
    finally:
        if tmpdir.exists():
            for f in tmpdir.glob("chunk_*.flac"):
                f.unlink()
            try:
                tmpdir.rmdir()
            except OSError:
                pass
    return out
