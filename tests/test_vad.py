"""VAD tests — SPEC Phase 5 acceptance: expected utterance counts ±1.
Fixtures are short edge-tts wavs with 2s silences between lines."""

import wave
from pathlib import Path

from phathom.audio.vad import FRAME_BYTES, Segmenter

FIX = Path(__file__).parent / "fixtures"


def run_file(path: Path, **kw) -> list:
    seg = Segmenter(**kw)
    out = []
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        t, data = 0.0, w.readframes(w.getnframes())
    for i in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES):
        utts = seg.feed(data[i:i + FRAME_BYTES], t)
        if utts:
            out.extend(utts)
        t += 0.03
    # flush: trailing silence ends any open utterance
    for _ in range(40):
        utts = seg.feed(bytes(FRAME_BYTES), t)
        if utts:
            out.extend(utts)
        t += 0.03
    return out


def test_two_utterances():
    utts = run_file(FIX / "fix2.wav")
    assert abs(len(utts) - 2) <= 1, f"expected ~2, got {len(utts)}"
    for u in utts:
        assert u.t_end > u.t_start and len(u.pcm) > 0


def test_three_utterances():
    utts = run_file(FIX / "fix3.wav")
    assert abs(len(utts) - 3) <= 1, f"expected ~3, got {len(utts)}"


def test_silence_gives_nothing():
    utts = run_file(FIX / "sil2.wav")
    assert utts == []


def test_short_blip_dropped():
    seg = Segmenter(min_ms=400)
    out = []
    t = 0.0
    # 200ms of loud non-speech energy is still long enough to trigger start;
    # use only 5 voiced-ish frames then silence: below start_frames=6 -> no utterance
    import struct
    loud = struct.pack("<480h", *([8000] * 480))
    for _ in range(5):
        assert seg.feed(loud, t) is None
        t += 0.03
    for _ in range(40):
        r = seg.feed(bytes(FRAME_BYTES), t)
        if r:
            out.extend(r)
        t += 0.03
    assert out == []
