"""WavSink (record tracks) + WsPlayer (bot voice over the extension).

Wire format (EXTENSION_SPEC §6.1) — binary frames:
    [1 byte kind][4 bytes uint32 LE seq][PCM s16le mono]
kind 0x01 = caller 16 kHz (ext → server), 0x02 = owner 16 kHz (ext → server),
kind 0x03 = speaker tap 16 kHz (ext → server),
kind 0x11 = bot speech 24 kHz (server → ext, preceded by ``play_begin`` JSON).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import struct
import wave
from pathlib import Path

log = logging.getLogger("phathom.server.ws_audio")

CALLER_RATE = 16000
BOT_RATE = 24000
# 100 ms bot chunks, paced at ~1.5x real time so stop_audio stays responsive.
BOT_CHUNK_S = 0.1

KIND_CALLER = 0x01
KIND_OWNER = 0x02
KIND_SPEAKER = 0x03
KIND_TAB = 0x04  # tab-capture audio 16 kHz (ext → server, §5.7)
KIND_BOT = 0x11


class WavSink:
    """Streaming 16 kHz s16le mono wav writer with silence padding."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(CALLER_RATE)
        self.bytes_written = 0
        self._closed = False

    def write(self, pcm: bytes) -> None:
        if not pcm or self._closed:
            return
        self._wav.writeframes(pcm)
        self.bytes_written += len(pcm)

    def seconds(self) -> float:
        return self.bytes_written / 2 / CALLER_RATE

    def pad_to(self, seconds: float) -> None:
        """Append digital silence until the track is ``seconds`` long (wall-clock alignment)."""
        missing = int(seconds * CALLER_RATE) * 2 - self.bytes_written
        if missing > 0:
            self.write(b"\x00" * missing)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._wav.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("closed %s (%.1fs)", self.path, self.seconds())


def _resample(samples: list[float], src_rate: int, dst_rate: int) -> bytes:
    """Linear resample of float mono samples to s16le bytes."""
    if src_rate == dst_rate:
        out = samples
    else:
        ratio = src_rate / dst_rate
        n_out = int(len(samples) / ratio)
        out = []
        for i in range(n_out):
            pos = i * ratio
            i0 = int(pos)
            i1 = min(len(samples) - 1, i0 + 1)
            frac = pos - i0
            out.append(samples[i0] * (1 - frac) + samples[i1] * frac)
    buf = bytearray(len(out) * 2)
    for i, v in enumerate(out):
        v = max(-1.0, min(1.0, v))
        struct.pack_into("<h", buf, i * 2, int(v * 0x7FFF) if v >= 0 else int(v * 0x8000))
    return bytes(buf)


def wav_file_to_bot_pcm(path: str | Path) -> bytes:
    """Read any PCM wav (e.g. 48 kHz TTS output) → 24 kHz s16le mono bytes."""
    with wave.open(str(path), "rb") as w:
        nch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        ints = struct.unpack(f"<{len(raw) // 2}h", raw)
        floats = [v / 0x8000 for v in ints]
    elif sw == 1:  # 8-bit unsigned
        floats = [(b - 128) / 128 for b in raw]
    else:
        raise ValueError(f"unsupported sample width: {sw}")
    if nch > 1:
        floats = [sum(floats[i:i + nch]) / nch for i in range(0, len(floats), nch)]
    return _resample(floats, sr, BOT_RATE)


class WsPlayer:
    """Same interface as audio/player.Player (play/interrupt/playing).

    ``agent.py`` needs no logic changes: duck-typing accepts this class.
    Bot audio is also recorded server-side via the optional ``on_pcm`` hook
    (WavSink.write of the 24 kHz stream is NOT wav-compatible; the session
    downsamples — here we forward raw chunks and let the session handle it).

    Take-over support: ``muted=True`` makes play() resolve False immediately
    (the bot stays silent) while recording continues.
    """

    def __init__(self, send_json, send_binary, on_pcm=None) -> None:
        self._send_json = send_json
        self._send_binary = send_binary
        self._on_pcm = on_pcm
        self.muted = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._seq = 0
        self._play_task: asyncio.Task | None = None

    @property
    def playing(self) -> bool:
        return self._play_task is not None and not self._play_task.done()

    async def play(self, wav_path: str | Path) -> bool:
        """Send the wav as 24 kHz bot chunks; True=finished, False=interrupted."""
        await self.interrupt()
        if self.muted:
            return False  # taken over: the bot stays silent, recording continues
        pid = next(self._ids)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[pid] = fut
        self._play_task = asyncio.create_task(self._stream(pid, wav_path, fut))
        try:
            return await fut
        finally:
            self._pending.pop(pid, None)

    async def _stream(self, pid: int, wav_path: str | Path, fut: asyncio.Future) -> None:
        try:
            pcm = wav_file_to_bot_pcm(wav_path)
        except Exception as e:  # noqa: BLE001
            log.warning("WsPlayer: cannot read %s: %r", wav_path, e)
            if not fut.done():
                fut.set_result(False)
            return
        frame_len = int(BOT_RATE * BOT_CHUNK_S) * 2
        try:
            await self._send_json({"t": "play_begin", "id": pid})
            for off in range(0, len(pcm), frame_len):
                chunk = pcm[off:off + frame_len]
                if self._on_pcm is not None:
                    try:
                        self._on_pcm(chunk)
                    except Exception:  # noqa: BLE001
                        pass
                hdr = struct.pack("<BI", KIND_BOT, self._seq & 0xFFFFFFFF)
                self._seq += 1
                await self._send_binary(hdr + chunk)
                await asyncio.sleep(BOT_CHUNK_S / 1.5)
            await self._send_json({"t": "play_end", "id": pid})
            # The extension acks with played{id}; fall back to done after a grace period.
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=30)
            except asyncio.TimeoutError:
                if not fut.done():
                    fut.set_result(True)
        except asyncio.CancelledError:
            if not fut.done():
                fut.set_result(False)
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("WsPlayer stream failed: %r", e)
            if not fut.done():
                fut.set_result(False)

    async def interrupt(self) -> None:
        active = (self._play_task is not None and not self._play_task.done()
                  or any(not f.done() for f in self._pending.values()))
        if self._play_task is not None and not self._play_task.done():
            self._play_task.cancel()
            try:
                await self._play_task
            except asyncio.CancelledError:
                pass
        for pid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(False)
        if not active:
            return  # idempotent: no stop_audio storm when nothing plays
        try:
            await self._send_json({"t": "stop_audio"})
        except Exception:  # noqa: BLE001
            pass

    def on_played(self, pid: int, completed: bool) -> None:
        fut = self._pending.get(pid)
        if fut is not None and not fut.done():
            fut.set_result(bool(completed))
