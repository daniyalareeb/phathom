"""WavSink alignment + wav→bot-PCM conversion."""

import struct
import wave

from phathom.server.ws_audio import (
    BOT_RATE,
    CALLER_RATE,
    WavSink,
    wav_file_to_bot_pcm,
)


def _sine_wav(path, rate=16000, secs=1.0, freq=440.0):
    n = int(rate * secs)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        import math
        for i in range(n):
            v = int(0.5 * 0x7FFF * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def test_sink_write_pad_close(tmp_path):
    sink = WavSink(tmp_path / "caller.wav")
    sink.write(b"\x01\x02" * 8000)  # 16000 bytes = 0.5 s
    assert sink.seconds() == 0.5
    sink.pad_to(1.0)  # wall-clock alignment with silence
    assert sink.seconds() == 1.0
    sink.close()
    with wave.open(str(tmp_path / "caller.wav"), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, CALLER_RATE)
        assert w.getnframes() == CALLER_RATE
    # valid header after close, padding is digital silence
    raw = (tmp_path / "caller.wav").read_bytes()
    assert raw[16000 + 44:] == b"\x00" * 16000


def test_bot_conversion_resamples_to_24k(tmp_path):
    p = tmp_path / "tts.wav"
    _sine_wav(p, rate=48000, secs=0.5)
    pcm = wav_file_to_bot_pcm(p)
    assert len(pcm) == int(BOT_RATE * 0.5) * 2
    # still a 440 Hz tone after resampling (Goertzel power at 440 >> 880).
    import math
    a = struct.unpack(f"<{len(pcm) // 2}h", pcm)

    def goertzel(freq):
        w = 2 * math.pi * freq / BOT_RATE
        c = 2 * math.cos(w)
        s0 = s1 = s2 = 0.0
        for v in a:
            s0 = v + c * s1 - s2
            s2, s1 = s1, s0
        return s1 * s1 + s2 * s2 - c * s1 * s2

    assert goertzel(440) > 100 * goertzel(880)
