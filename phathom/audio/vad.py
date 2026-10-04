"""Speech segmentation over 30ms frames — SPEC §9.5. Pure Python + webrtcvad, no I/O."""

import logging
from collections import deque
from dataclasses import dataclass

import webrtcvad

log = logging.getLogger("phathom.audio.vad")

FRAME_MS = 30
FRAME_BYTES = 960  # 30ms at 16 kHz, 16-bit mono


@dataclass
class Utterance:
    pcm: bytes          # 16 kHz s16le mono
    t_start: float      # seconds since call start
    t_end: float


class Segmenter:
    def __init__(self, aggressiveness: int = 2, start_frames: int = 6,
                 end_silence_ms: int = 700, min_ms: int = 400,
                 max_ms: int = 20000, preroll_ms: int = 300):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.start_frames = start_frames
        self.end_silence_frames = max(1, end_silence_ms // FRAME_MS)
        self.min_frames = max(1, min_ms // FRAME_MS)
        self.max_frames = max(1, max_ms // FRAME_MS)
        self.preroll_frames = preroll_ms // FRAME_MS
        self._recent: deque = deque(maxlen=10)  # (frame, t, voiced)
        self._pre: deque = deque(maxlen=max(1, self.preroll_frames))  # preroll audio
        self._active: list[tuple[bytes, float]] = []  # (frame, t) of open utterance
        self._silence = 0
        self._speaking = False

    @property
    def speaking(self) -> bool:
        """True while an utterance is in progress (used for barge-in)."""
        return self._speaking

    def feed(self, frame: bytes, t: float) -> list[Utterance] | None:
        if len(frame) != FRAME_BYTES:
            return None
        try:
            voiced = self.vad.is_speech(frame, 16000)
        except Exception:  # noqa: BLE001
            voiced = False
        self._recent.append(voiced)
        done: list[Utterance] = []

        if not self._speaking:
            self._pre.append((frame, t))
            if sum(1 for v in self._recent if v) >= self.start_frames and len(self._recent) >= self.start_frames:
                # utterance starts; include preroll audio from before the trigger
                self._speaking = True
                self._active = list(self._pre)
                self._silence = 0
            return None

        self._active.append((frame, t))
        if voiced:
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.end_silence_frames or len(self._active) >= self.max_frames:
            done = self._emit()
        return done or None

    def _emit(self) -> list[Utterance]:
        frames = self._active
        self._active = []
        self._silence = 0
        self._speaking = False
        self._pre.clear()
        # trim trailing silence frames
        end_idx = len(frames)
        while end_idx > 0 and not self._is_voiced_frame(frames[end_idx - 1][0]):
            end_idx -= 1
        frames = frames[:end_idx]
        if len(frames) < self.min_frames:
            return []  # drop utterances shorter than min_ms
        pcm = b"".join(f for f, _ in frames)
        return [Utterance(pcm=pcm, t_start=frames[0][1], t_end=frames[-1][1] + FRAME_MS / 1000)]

    def _is_voiced_frame(self, frame: bytes) -> bool:
        try:
            return self.vad.is_speech(frame, 16000)
        except Exception:  # noqa: BLE001
            return False
