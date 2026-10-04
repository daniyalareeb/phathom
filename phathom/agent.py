"""Live conversation loop (assistant mode) — SPEC §9.9."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from phathom.ai.llm import AgentDecision, LiveContext, Turn, live_reply
from phathom.ai.prompts import fixed_line
from phathom.ai.stt import is_hallucination, transcribe_pcm
from phathom.ai.tts import synth
from phathom.audio.player import Player
from phathom.audio.vad import FRAME_BYTES, Segmenter, Utterance
from phathom.config import settings
from phathom.store import Store

log = logging.getLogger("phathom.agent")

BARGE_IN_S = 0.3  # caller must talk over the bot this long before playback stops


@dataclass
class AgentResult:
    message_for_owner: str | None = None
    end_reason: str = "done"  # done|peer_hangup|stopped|max_time|silence|failed


class LiveAgent:
    def __init__(self, *, call_id: str, caller: str, contact_notes: str | None, brief: str,
                 frame_queue: asyncio.Queue, player: Player, store: Store | None,
                 call_active, greet_lang: str = "en"):
        self.call_id = call_id
        self.caller = caller
        self.contact_notes = contact_notes
        self.brief = brief
        self.frames = frame_queue
        self.player = player
        self.store = store
        self.call_active = call_active  # async () -> bool
        self.greet_lang = greet_lang if greet_lang in ("en", "ur") else "en"
        self.segmenter = Segmenter()
        self.history: list[Turn] = []
        self.bot_segments: list[dict] = []  # in-memory (used when DB is down)
        self.message_for_owner: str | None = None
        self._cancel = False
        # Utterances heard while a turn is in progress are merged here (overlap rule, SPEC §9.9).
        self._pending: Utterance | None = None
        self._utt_ready = asyncio.Event()
        self._recent_texts: list[str] = []
        self._failures = 0
        self._t0 = time.monotonic()
        self._last_activity = time.monotonic()
        self._speaking_since: float | None = None
        self._latencies: list[float] = []

    def cancel(self) -> None:
        """Stop now, including mid-sentence (Disconnect / phathom stop)."""
        self._cancel = True
        if self.player.playing:
            asyncio.get_running_loop().create_task(self.player.interrupt())

    def _now(self) -> float:
        return time.monotonic() - self._t0

    async def _save_segment(self, speaker: str, text: str, t_start: float, t_end: float) -> None:
        seg = {"t_start": t_start, "t_end": t_end, "speaker": speaker,
               "text": text, "source": "live"}
        if speaker == "phathom":
            self.bot_segments.append(dict(seg))
        if self.store is not None:
            try:
                await self.store.add_segments(self.call_id, [seg])
            except Exception as e:  # noqa: BLE001
                log.warning("segment save failed: %r", e)

    async def _speak(self, text: str, lang: str, t_start: float) -> None:
        """Synth + play; on Urdu TTS failure say the English fallback line (§9.7)."""
        say, say_lang = text, lang
        try:
            wav = await synth(say, say_lang)
        except Exception as e:  # noqa: BLE001
            log.warning("TTS failed (%r); English fallback line", e)
            say, say_lang = fixed_line("fallback", "en"), "en"
            try:
                wav = await synth(say, say_lang)
            except Exception as e2:  # noqa: BLE001
                log.warning("fallback TTS also failed: %r", e2)
                return
        finished = await self.player.play(Path(wav))
        shown = say if finished else say + " [interrupted]"
        await self._save_segment("phathom", shown, t_start, self._now())
        self.history.append(Turn(role="bot", text=json.dumps(
            {"say": say, "lang": say_lang, "end_call": False,
             "message_for_owner": self.message_for_owner})))
        self._last_activity = time.monotonic()

    async def _handle_utterance(self, utt: Utterance) -> str | None:
        """One full turn. Returns 'end' if the call should end, else None."""
        t_utt0 = time.monotonic()
        text = await transcribe_pcm(utt.pcm, settings.STT_MODEL_LIVE)
        stt_ms = (time.monotonic() - t_utt0) * 1000
        dur = utt.t_end - utt.t_start
        if not text or is_hallucination(text, dur, self._recent_texts):
            return None
        self._recent_texts.append(text)
        self._last_activity = time.monotonic()
        await self._save_segment("caller", text, utt.t_start, utt.t_end)
        self.history.append(Turn(role="caller", text=text))

        ctx = LiveContext(brief=self.brief, caller=self.caller,
                          contact_notes=self.contact_notes,
                          now=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"))
        t_llm0 = time.monotonic()
        try:
            decision = await live_reply(ctx, self.history)
            self._failures = 0
        except Exception as e:  # noqa: BLE001
            log.warning("live turn failed (%r); fallback line", e)
            self._failures += 1
            if self._failures >= 3:
                log.warning("3 AI failures in a row; goodbye + end")
                await self._speak(fixed_line("goodbye", self.greet_lang), self.greet_lang, self._now())
                return "end"
            decision = AgentDecision(say=fixed_line("fallback", self.greet_lang),
                                     lang=self.greet_lang, end_call=False,
                                     message_for_owner=self.message_for_owner)
        llm_ms = (time.monotonic() - t_llm0) * 1000
        if decision.message_for_owner is not None:
            self.message_for_owner = decision.message_for_owner
        if decision.say:
            t_tts0 = time.monotonic()
            await self._speak(decision.say, decision.lang, self._now())
            tts_ms = (time.monotonic() - t_tts0) * 1000
        else:
            tts_ms = 0.0
        total = (time.monotonic() - t_utt0) * 1000
        self._latencies.append(total)
        log.info("turn stt=%dms llm=%dms tts=%dms total=%dms say=%r",
                 stt_ms, llm_ms, tts_ms, total, decision.say[:80])
        if decision.end_call:
            while self.player.playing and not self._cancel:
                await asyncio.sleep(0.2)
            return "end"
        return None

    def _queue_utterance(self, u: Utterance) -> None:
        """Overlap rule: if a turn hasn't picked up the pending audio yet, merge into it."""
        if self._pending is None:
            self._pending = u
        else:
            self._pending = Utterance(self._pending.pcm + u.pcm, self._pending.t_start, u.t_end)
        self._last_activity = time.monotonic()
        self._utt_ready.set()

    def _take_pending(self) -> Utterance | None:
        u, self._pending = self._pending, None
        self._utt_ready.clear()
        return u

    async def _listen(self) -> None:
        """Runs for the whole call, also while a turn is being processed or the bot is speaking,
        so barge-in works and no caller audio is dropped."""
        while True:
            frame = await self.frames.get()
            if len(frame) != FRAME_BYTES:
                continue
            utts = self.segmenter.feed(frame, self._now()) or []
            if self.player.playing and self.segmenter.speaking:
                if self._speaking_since is None:
                    self._speaking_since = time.monotonic()
                elif time.monotonic() - self._speaking_since >= BARGE_IN_S:
                    log.info("barge-in: interrupting playback")
                    await self.player.interrupt()
                    self._speaking_since = None
            else:
                self._speaking_since = None
            for u in utts:
                self._queue_utterance(u)

    async def run(self) -> AgentResult:
        await asyncio.sleep(1.0)  # settle after connect
        if self._cancel or not await self._safe_active():
            return AgentResult(self.message_for_owner, "peer_hangup")
        listener = asyncio.create_task(self._listen())
        try:
            return await self._turns()
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def _turns(self) -> AgentResult:
        greeting = fixed_line("greeting", self.greet_lang)
        self.history.append(Turn(role="bot", text=json.dumps(
            {"say": greeting, "lang": self.greet_lang, "end_call": False, "message_for_owner": None})))
        await self._speak(greeting, self.greet_lang, self._now())

        while not self._cancel:
            if not await self._safe_active():
                return AgentResult(self.message_for_owner, "peer_hangup")
            if self._now() > settings.MAX_CALL_S:
                await self._speak(fixed_line("goodbye", self.greet_lang), self.greet_lang, self._now())
                return AgentResult(self.message_for_owner, "max_time")
            if (not self.player.playing and not self.segmenter.speaking
                    and time.monotonic() - self._last_activity > settings.SILENCE_HANGUP_S):
                log.info("two-way silence timeout; goodbye + end")
                await self._speak(fixed_line("goodbye", self.greet_lang), self.greet_lang, self._now())
                return AgentResult(self.message_for_owner, "silence")
            try:
                await asyncio.wait_for(self._utt_ready.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            utt = self._take_pending()
            if utt is None:
                continue
            if await self._handle_utterance(utt) == "end":
                return AgentResult(self.message_for_owner, "done")
        return AgentResult(self.message_for_owner, "stopped")

    async def _safe_active(self) -> bool:
        try:
            return bool(await self.call_active())
        except Exception as e:  # noqa: BLE001
            log.warning("call_active check: %r", e)
            return True
