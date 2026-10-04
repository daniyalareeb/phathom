"""TTS playback into phathom_voice — SPEC §9.6. `paplay --device=phathom_voice <wav>`."""

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("phathom.audio")


class Player:
    def __init__(self, device: str = "phathom_voice"):
        self.device = device
        self._proc: asyncio.subprocess.Process | None = None
        self._interrupted = False

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def play(self, wav_path: Path) -> bool:
        """Play a wav. A new play() interrupts the current one. True=finished, False=interrupted."""
        await self.interrupt()
        self._interrupted = False
        self._proc = await asyncio.create_subprocess_exec(
            "paplay", f"--device={self.device}", str(wav_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        log.info("playing %s on %s", wav_path, self.device)
        await self._proc.wait()
        done = not self._interrupted
        self._proc = None
        return done

    async def interrupt(self) -> None:
        if self.playing:
            assert self._proc is not None
            self._interrupted = True
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
