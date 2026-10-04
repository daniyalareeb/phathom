"""Call state machine + pure policy function (EXTENSION_SPEC §3, §6.2).

- ``decide()`` is pure (mode/contacts/state in, action out) and fully tested.
- ``CallSession`` owns one call: recording (WavSink per track), bot behaviour
  per mode (talk = LiveAgent + WsPlayer, listen = disclosure + record,
  copilot = record only), live transcript events, and after-call pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from phathom.agent import LiveAgent
from phathom.ai.prompts import fixed_line
from phathom.ai.stt import is_hallucination, transcribe_pcm
from phathom.ai.tts import synth
from phathom.audio.vad import FRAME_BYTES, Segmenter
from phathom.config import settings
from phathom.server import runtime_settings
from phathom.server.caller_pick import OWNER_KEY, pick_caller, resolve_source
from phathom.server.ws_audio import KIND_BOT, WavSink, WsPlayer

log = logging.getLogger("phathom.server.session")

# state.json mode -> UI answer-for-me value
ANSWER_OF = {"off": "off", "notes": "listen", "assistant": "talk"}
# bot modes use these call-mode labels in the DB
DB_MODE_OF = {"talk": "assistant", "listen": "notes", "copilot": "copilot"}


def decide(event: str, *, connected: bool, answer_mode: str,
           copilot: bool, blocked: bool) -> str:
    """Pure policy (§3 table). Returns: ignore | answer_after_delay | record_copilot | nothing.

    event: "ring" | "owner_answered" (ring vanished, call active) | "outgoing".
    answer_mode: "off" | "listen" | "talk".
    """
    if event == "ring":
        if not connected:
            return "ignore"
        if answer_mode == "off":
            return "ignore"
        if blocked:
            return "ignore"
        return "answer_after_delay"
    if event in ("owner_answered", "outgoing"):
        if not connected:
            return "nothing"
        if copilot and not blocked:
            return "record_copilot"
        return "nothing"
    raise ValueError(f"unknown event: {event}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LiveSegment:
    speaker: str
    text: str
    t: float


class CallSession:
    """One call from ``call_started`` to pipeline handoff."""

    def __init__(self, *, call_id: str, peer: str, direction: str, kind: str,
                 started_at: str, audio_dir: Path,
                 send_json, send_binary, publish,
                 store_factory, live_transcript: bool = True) -> None:
        assert kind in ("talk", "listen", "copilot"), kind
        self.call_id = call_id
        self.peer = peer
        self.direction = direction
        self.kind = kind
        self.started_at = started_at
        self.audio_dir = Path(audio_dir)
        self._send_json = send_json
        self._send_binary = send_binary
        self._publish = publish
        self._store_factory = store_factory
        self.live_transcript = live_transcript
        self.open = True
        self.end_reason: str | None = None
        self._t0 = time.monotonic()
        self.sinks = {
            "caller": WavSink(self.audio_dir / "caller.wav"),
            "owner": WavSink(self.audio_dir / "owner.wav"),
            "speaker": WavSink(self.audio_dir / "speaker.wav"),
            "tab": WavSink(self.audio_dir / "tab.wav"),
        }
        # §5.7: one wav per speaker:N tap label, kept 7 days under taps/.
        self.taps_dir = self.audio_dir / "taps"
        self.taps_dir.mkdir(parents=True, exist_ok=True)
        self.extra_sinks: dict[str, WavSink] = {}
        self.tap_labels: dict[str, str] = {}  # safe stem -> full track label
        self._bot_chunks: list[bytes] = []  # 24 kHz chunks, converted at finalize
        self.agent_queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        self._live_queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        self._player = WsPlayer(send_json, send_binary, on_pcm=self._bot_chunks.append)
        self._agent: LiveAgent | None = None
        self._tasks: list[asyncio.Task] = []
        self._rate_limited_until = 0.0
        self.live_segments: list[LiveSegment] = []
        # ---- §5.7 caller-source state ----
        import webrtcvad as _vad_mod
        self._vad = _vad_mod.Vad(2)
        self.buckets: dict[int, dict[str, int]] = {}  # 100 ms bucket -> {label: voiced}
        self.webrtc_seen = False
        self.tab_seen = False
        self.active_source: str | None = None  # "webrtc" | "tab" | "speaker:<label>"
        self.tap_db: dict[str, float] = {}  # label -> last batch dBFS (Diagnostics meters)
        self._last_pick_log = ""
        # Take over (B4): bot muted + agent stops getting frames; sinks keep recording.
        self.taken_over = False

    # ---- ingress (called by the WS handler) ----

    def _speaker_sink(self, track: str) -> WavSink:
        if track == "speaker":
            return self.sinks["speaker"]
        safe = "".join(c.lower() if c.isalnum() else "_" for c in track).strip("_")[:48]
        if safe not in self.extra_sinks:
            self.extra_sinks[safe] = WavSink(self.taps_dir / f"{safe}.wav")
            log.info("new speaker tap %r -> taps/%s.wav", track, safe)
        self.tap_labels[safe] = track
        return self.extra_sinks[safe]

    def _vad_frames(self, pcm: bytes) -> int:
        """Count voiced 30 ms frames in a PCM chunk (16 kHz s16le)."""
        voiced = 0
        for off in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            try:
                if self._vad.is_speech(pcm[off:off + FRAME_BYTES], 16000):
                    voiced += 1
            except Exception:  # noqa: BLE001
                pass
        return voiced

    def _note_bucket(self, label: str, voiced: int) -> None:
        bucket = int(self._elapsed() * 10)
        b = self.buckets.setdefault(bucket, {})
        b[label] = b.get(label, 0) + voiced
        # Bound memory: keep ~60 s of buckets.
        if len(self.buckets) > 700:
            for k in sorted(self.buckets)[: len(self.buckets) - 700]:
                del self.buckets[k]

    def _recent_buckets(self, seconds: float = 5.0) -> list[dict[str, int]]:
        cutoff = int(self._elapsed() * 10) - int(seconds * 10)
        return [b for k, b in sorted(self.buckets.items()) if k >= cutoff]

    def reevaluate_source(self) -> str | None:
        """Re-run the §5.7 auto-pick over the last 5 s. Called every 5 s live."""
        try:
            mode = str(runtime_settings.get("caller_source"))
        except Exception:  # noqa: BLE001
            mode = "auto"
        src = resolve_source(mode=mode, webrtc_seen=self.webrtc_seen,
                             tab_seen=self.tab_seen, buckets=self._recent_buckets())
        if src != self.active_source:
            self.active_source = src
            msg = f"caller source -> {src} (mode={mode})"
            if msg != self._last_pick_log:
                self._last_pick_log = msg
                log.info(msg)
        return self.active_source

    def _track_selected(self, track: str) -> bool:
        if self.taken_over:
            return False  # taken over: agent stops listening; recording continues
        src = self.active_source or self.reevaluate_source()
        if src == "webrtc":
            return track == "caller" or track.startswith("caller")
        if src == "tab":
            return track == "tab" or track.startswith("tab")
        if src and src.startswith("speaker:"):
            return track == src[len("speaker:") :]
        # No source yet: feed caller/webrtc frames so talk mode works instantly.
        return track == "caller" or track.startswith("caller")

    def feed(self, track: str, pcm: bytes) -> None:
        if not self.open or not pcm:
            return
        voiced = self._vad_frames(pcm)
        # Diagnostics level meter (last-batch dBFS per label).
        try:
            import struct as _st
            n = len(pcm) // 2
            if n:
                vals = _st.unpack(f"<{n}h", pcm[: n * 2])
                ms = sum((v / 0x8000) ** 2 for v in vals) / n
                import math as _math
                self.tap_db[track] = round(10 * _math.log10(ms), 1) if ms > 0 else -999.0
        except Exception:  # noqa: BLE001
            pass
        if track == "owner" or track.startswith("owner"):
            self.sinks["owner"].write(pcm)
            self._note_bucket(OWNER_KEY, voiced)
            self._offer(self._live_queue, ("owner", pcm))
        elif track == "caller" or track.startswith("caller"):
            self.webrtc_seen = True
            self.sinks["caller"].write(pcm)
            self._note_bucket("caller", voiced)
            self._offer(self._live_queue, ("caller", pcm))
            if self._track_selected(track):
                self._offer(self.agent_queue, pcm)
        elif track == "tab" or track.startswith("tab"):
            self.tab_seen = True
            self.sinks["tab"].write(pcm)
            self._note_bucket("tab", voiced)
            self._offer(self._live_queue, ("caller", pcm))
            if self._track_selected(track):
                self._offer(self.agent_queue, pcm)
        elif track.startswith("speaker"):
            self._speaker_sink(track).write(pcm)
            self._note_bucket(track, voiced)
            self._offer(self._live_queue, ("caller", pcm))
            if self._track_selected(track):
                self._offer(self.agent_queue, pcm)
        else:
            self.sinks["caller"].write(pcm)
            self._note_bucket("caller", voiced)
            if not self.taken_over:
                self._offer(self.agent_queue, pcm)
            self._offer(self._live_queue, ("caller", pcm))

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def on_played(self, pid: int, completed: bool) -> None:
        self._player.on_played(pid, completed)

    def close_ingest(self, reason: str = "ended") -> None:
        self.open = False
        self.end_reason = reason
        if self._agent is not None:
            self._agent.cancel()

    # ---- egress commands ----

    async def take_over(self) -> None:
        self.taken_over = True
        self._player.muted = True
        await self._send_json({"t": "set_gain", "mic": 1, "bot": 1})
        await self._player.interrupt()

    async def hand_back(self) -> None:
        self.taken_over = False
        self._player.muted = False
        await self._send_json({"t": "set_gain", "mic": 0, "bot": 1})

    async def hangup(self) -> None:
        await self._send_json({"t": "hangup"})

    # ---- run ----

    def _elapsed(self) -> float:
        return time.monotonic() - self._t0

    async def _call_active(self) -> bool:
        return self.open

    async def run(self) -> dict:
        """Run the session to completion; returns a summary dict for tests/logs."""
        store = None
        try:
            store = await self._store_factory()
        except Exception as e:  # noqa: BLE001 — SPEC §12: answer/record even if DB is down
            log.warning("store unavailable at call start (%r); recording only", e)
        if store is not None:
            try:
                await store.create_call(self.call_id, self.peer, self.started_at,
                                        DB_MODE_OF[self.kind], status="live",
                                        audio_dir=str(self.audio_dir))
            except Exception as e:  # noqa: BLE001
                log.warning("create_call failed: %r", e)
        if self.kind in ("talk", "listen"):
            await self._send_json({"t": "set_gain", "mic": 0, "bot": 1})
        await self._publish({"t": "live", "call_id": self.call_id,
                             "event": {"type": "call_started", "kind": self.kind,
                                       "peer": self.peer, "direction": self.direction}})
        if self.live_transcript:
            self._tasks.append(asyncio.create_task(self._live_transcriber()))
        self._tasks.append(asyncio.create_task(self._picker()))
        try:
            if self.kind == "talk":
                await self._run_talk(store)
            elif self.kind == "listen":
                await self._run_listen(store)
            else:
                await self._run_copilot()
        finally:
            for t in self._tasks:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
            result = await self._finalize(store)
            if store is not None:
                try:
                    await store.close()
                except Exception:  # noqa: BLE001
                    pass
            return result

    async def _run_talk(self, store) -> None:
        import yaml  # local import: contacts file access only needed here
        from phathom import contacts as contacts_mod
        brief = ""
        try:
            brief = Path("brief.md").read_text()
        except OSError:
            pass
        notes = contacts_mod.notes_for(self.peer)
        if store is not None:
            # §8.4: brain summary appended to contact_notes (cap 1,500 chars).
            # agent.py itself is untouched — only the notes string changes.
            try:
                from phathom import brain as brain_mod
                brain_ctx = await brain_mod.brain_context_for(store, self.peer)
                if brain_ctx:
                    notes = ((notes + "\n" if notes else "") + brain_ctx)
            except Exception as e:  # noqa: BLE001
                log.warning("brain context lookup failed: %r", e)
        self._agent = LiveAgent(
            call_id=self.call_id, caller=self.peer, contact_notes=notes, brief=brief,
            frame_queue=self.agent_queue, player=self._player, store=store,
            call_active=self._call_active)
        res = await self._agent.run()
        log.info("talk agent ended: %s", res.end_reason)
        self.close_ingest(res.end_reason)

    async def _run_listen(self, store) -> None:
        if runtime_settings.get("disclosure"):
            try:
                wav = await synth(fixed_line("disclosure", "en"), "en")
                await self._player.play(Path(wav))
            except Exception as e:  # noqa: BLE001
                log.warning("disclosure playback failed: %r", e)
        max_s = float(runtime_settings.get("max_call_s"))
        while self.open and self._elapsed() < max_s:
            await asyncio.sleep(0.5)

    async def _run_copilot(self) -> None:
        max_s = float(runtime_settings.get("owner_call_max_s"))
        while self.open and self._elapsed() < max_s:
            await asyncio.sleep(0.5)

    async def _picker(self) -> None:
        """Re-evaluate the §5.7 auto-pick every 5 s during the call."""
        while self.open:
            await asyncio.sleep(5)
            if not self.open:
                break
            try:
                self.reevaluate_source()
            except Exception:  # noqa: BLE001
                pass

    async def _live_transcriber(self) -> None:
        segmenters = {"caller": Segmenter(), "owner": Segmenter()}
        recent: list[str] = []
        model = settings.STT_MODEL_LIVE
        while self.open:
            try:
                speaker, frame = await asyncio.wait_for(self._live_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if time.monotonic() < self._rate_limited_until:
                continue
            seg = segmenters.get(speaker)
            if seg is None or len(frame) != FRAME_BYTES:
                continue
            try:
                utts = seg.feed(frame, self._elapsed()) or []
            except Exception:  # noqa: BLE001
                continue
            for utt in utts:
                try:
                    text = await transcribe_pcm(utt.pcm, model)
                except Exception as e:  # noqa: BLE001
                    if "429" in repr(e) or "rate" in repr(e).lower():
                        self._rate_limited_until = time.monotonic() + 60
                        await self._publish({"t": "live", "call_id": self.call_id, "event": {
                            "type": "transcript_paused", "reason": "rate limit"}})
                    continue
                dur = utt.t_end - utt.t_start
                if not text or is_hallucination(text, dur, recent):
                    continue
                recent.append(text)
                ev = {"type": "transcript", "speaker": speaker, "text": text,
                      "t": round(utt.t_start, 1)}
                self.live_segments.append(LiveSegment(speaker, text, utt.t_start))
                await self._publish({"t": "live", "call_id": self.call_id, "event": ev})

    # ---- finalize ----

    def _repick_caller(self) -> None:
        """End-of-call re-pick: run the auto-pick over the WHOLE call and, if a
        different tap clearly wins, use it as caller.wav for the pipeline
        (the live choice may have been wrong). The live file is kept as
        caller_live.wav. Never crashes the finalize path."""
        try:
            import shutil
            all_buckets = [b for _, b in sorted(self.buckets.items())]
            if not all_buckets:
                return
            winner = pick_caller(all_buckets)
            totals: dict[str, int] = {}
            for b in all_buckets:
                for label, v in b.items():
                    if label != OWNER_KEY:
                        totals[label] = totals.get(label, 0) + v
            if not winner or totals.get(winner, 0) == 0:
                log.info("repick: no voiced tap; keeping live caller.wav")
                return
            src_path = None
            if winner == "caller":
                log.info("repick: webrtc caller wins (%d voiced); keeping caller.wav",
                         totals[winner])
                return
            elif winner == "tab":
                src_path = self.audio_dir / "tab.wav"
            else:
                safe = "".join(c.lower() if c.isalnum() else "_" for c in winner).strip("_")[:48]
                src_path = self.taps_dir / f"{safe}.wav"
            caller_wav = self.audio_dir / "caller.wav"
            if src_path and src_path.exists():
                live_voiced = totals.get("caller", 0) + totals.get("tab", 0)
                log.info("repick: %r wins (%d voiced vs live %d); using it as caller.wav",
                         winner, totals[winner], live_voiced)
                if caller_wav.exists():
                    shutil.copy(caller_wav, self.audio_dir / "caller_live.wav")
                shutil.copy(src_path, caller_wav)
            else:
                log.info("repick winner %r has no wav; keeping live caller.wav", winner)
        except Exception as e:  # noqa: BLE001
            log.warning("repick failed (keeping live caller.wav): %r", e)

    def _write_bot_wav(self) -> Path | None:
        """Convert recorded 24 kHz bot chunks to a 16 kHz bot.wav."""
        if not self._bot_chunks:
            return None
        import struct as _struct
        raw = b"".join(self._bot_chunks)
        n = len(raw) // 2
        ints = _struct.unpack(f"<{n}h", raw[:n * 2])
        floats = [v / 0x8000 for v in ints]
        # 24 kHz -> 16 kHz: take 2 of every 3 with linear blend.
        out = bytearray((n * 2 // 3) * 2)
        for i in range(len(out) // 2):
            pos = i * 1.5
            i0 = int(pos)
            i1 = min(n - 1, i0 + 1)
            v = floats[i0] * (1 - (pos - i0)) + floats[i1] * (pos - i0)
            v = max(-1.0, min(1.0, v))
            _struct.pack_into("<h", out, i * 2, int(v * 0x7FFF) if v >= 0 else int(v * 0x8000))
        path = self.audio_dir / "bot.wav"
        import wave as _wave
        with _wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(bytes(out))
        return path

    async def _finalize(self, store) -> dict:
        elapsed = self._elapsed()
        for sink in list(self.sinks.values()) + list(self.extra_sinks.values()):
            sink.pad_to(elapsed)
            sink.close()
        # Whole-call re-pick AFTER sinks close (it replaces caller.wav) — §5.7.
        self._repick_caller()
        bot_wav = self._write_bot_wav()
        result = {"call_id": self.call_id, "kind": self.kind,
                  "reason": self.end_reason or "ended", "duration_s": round(elapsed, 1)}
        await self._publish({"t": "live", "call_id": self.call_id,
                             "event": {"type": "call_ended", "reason": result["reason"]}})
        if store is None:
            log.warning("no store; skipping pipeline for %s (audio kept)", self.call_id)
            return result
        try:
            from phathom import pipeline as pipeline_mod
            ended_at = now_iso()
            caller_wav = self.audio_dir / "caller.wav"
            owner_wav = self.audio_dir / "owner.wav"
            if self.kind == "copilot":
                # Copilot: both human sides; caller.wav + owner.wav.
                await pipeline_mod.run_pipeline(
                    self.call_id, caller_label=self.peer, mode="copilot",
                    started_at=self.started_at, audio_dir=self.audio_dir,
                    caller_wav=caller_wav if caller_wav.exists() else None,
                    owner_wav=owner_wav if owner_wav.exists() else None,
                    live_bot_segments=[],
                    store=store, ended_at=ended_at)
            else:
                bot_segs = list(self._agent.bot_segments) if self._agent else []
                await pipeline_mod.run_pipeline(
                    self.call_id, caller_label=self.peer, mode=DB_MODE_OF[self.kind],
                    started_at=self.started_at, audio_dir=self.audio_dir,
                    caller_wav=caller_wav if caller_wav.exists() else None,
                    bot_wav=bot_wav, live_bot_segments=bot_segs,
                    disclosure_played=(self.kind == "listen"),
                    store=store, ended_at=ended_at)
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline handoff failed for %s: %r", self.call_id, e)
        await self._publish({"t": "live", "call_id": self.call_id,
                             "event": {"type": "summary_ready"}})
        return result
