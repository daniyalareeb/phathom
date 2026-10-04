"""Single-track pulse recorder — SPEC §9.4.

Streams one pulse source into (a) a wav file and (b) an asyncio.Queue of 30ms frames.
`parec -d <source> --format=s16le --rate=16000 --channels=1 --raw`, 960-byte chunks.
"""

import asyncio
import logging
import shutil
import wave
from pathlib import Path

log = logging.getLogger("phathom.audio")

FRAME_BYTES = 960  # 30ms at 16 kHz, 16-bit mono
QUEUE_MAX = 200


class TrackRecorder:
    def __init__(self, source: str, wav_path: Path, frame_queue: asyncio.Queue[bytes] | None = None):
        self.source = source
        self.wav_path = Path(wav_path)
        self.frame_queue = frame_queue
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._wav: wave.Wave_write | None = None

    def _command(self) -> list[str]:
        if shutil.which("parec"):
            return ["parec", "-d", self.source, "--format=s16le", "--rate=16000", "--channels=1", "--raw"]
        # fallback per SPEC §9.4
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse",
                "-i", self.source, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]

    async def start(self) -> None:
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.wav_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(16000)
        self._proc = await asyncio.create_subprocess_exec(
            *self._command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self._task = asyncio.create_task(self._pump())
        log.info("recording %s -> %s", self.source, self.wav_path)

    async def _pump(self) -> None:
        assert self._proc and self._proc.stdout and self._wav
        try:
            while True:
                chunk = await self._proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                self._wav.writeframes(chunk)
                if self.frame_queue is not None:
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        self.frame_queue.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
        if self._wav:
            self._wav.close()  # flush + valid header
            self._wav = None
        log.info("stopped recording %s", self.wav_path)
