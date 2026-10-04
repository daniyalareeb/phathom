"""Live agent: barge-in and listening while the bot speaks (no network, no audio devices)."""

import asyncio
import wave
from pathlib import Path

from phathom import agent as agent_mod
from phathom.ai.llm import AgentDecision
from phathom.audio.vad import FRAME_BYTES

FIX = Path(__file__).parent / "fixtures"


class FakePlayer:
    """'Plays' for `seconds` unless interrupted."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.interrupts = 0
        self._stop: asyncio.Event | None = None

    @property
    def playing(self) -> bool:
        return self._stop is not None

    async def play(self, wav) -> bool:
        self._stop = asyncio.Event()
        try:
            await asyncio.wait_for(self._stop.wait(), self.seconds)
            return False
        except asyncio.TimeoutError:
            return True
        finally:
            self._stop = None

    async def interrupt(self) -> None:
        if self._stop is not None:
            self.interrupts += 1
            self._stop.set()


def speech_frames() -> list[bytes]:
    with wave.open(str(FIX / "fix2.wav"), "rb") as w:
        data = w.readframes(w.getnframes())
    return [data[i:i + FRAME_BYTES] for i in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES)]


def make_agent(monkeypatch, player, heard: list[str]):
    async def fake_stt(pcm, model, prompt=None):
        heard.append(f"{len(pcm)}")
        return "hello there"

    async def fake_reply(ctx, history):
        return AgentDecision(say="okay", lang="en")

    async def fake_synth(text, lang):
        return Path("unused.wav")

    async def active():
        return True

    monkeypatch.setattr(agent_mod, "transcribe_pcm", fake_stt)
    monkeypatch.setattr(agent_mod, "live_reply", fake_reply)
    monkeypatch.setattr(agent_mod, "synth", fake_synth)
    monkeypatch.setattr(agent_mod, "is_hallucination", lambda *a: False)
    return agent_mod.LiveAgent(
        call_id="t", caller="Test", contact_notes=None, brief="", frame_queue=asyncio.Queue(maxsize=200),
        player=player, store=None, call_active=active)


async def feed_realtime(q: asyncio.Queue, frames: list[bytes], pace: float = 0.003) -> None:
    for f in frames:
        await q.put(f)
        await asyncio.sleep(pace)  # 0.003 = ~10x real time; keeps the listener busy like a live stream


async def test_caller_talking_over_greeting_interrupts_it(monkeypatch):
    player = FakePlayer(seconds=30)  # greeting would otherwise play for 30s
    heard: list[str] = []
    agent = make_agent(monkeypatch, player, heard)
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(1.3)  # agent settles 1s, then starts the greeting
    assert player.playing
    # Barge-in needs BARGE_IN_S of wall-clock speech, so feed at ~real time (30 ms frames).
    await feed_realtime(agent.frames, speech_frames() + [bytes(FRAME_BYTES)] * 40, pace=0.025)
    await asyncio.sleep(0.5)
    agent.cancel()
    await asyncio.wait_for(task, 5)
    assert player.interrupts >= 1, "barge-in never interrupted the greeting"
    assert heard, "caller speech during the greeting was never transcribed"


async def test_speech_during_a_turn_is_merged_not_dropped(monkeypatch):
    player = FakePlayer(seconds=0.01)
    heard: list[str] = []
    agent = make_agent(monkeypatch, player, heard)
    slow_stt_started = asyncio.Event()

    async def slow_stt(pcm, model, prompt=None):
        heard.append(f"{len(pcm)}")
        slow_stt_started.set()
        await asyncio.sleep(1.0)  # caller keeps talking while we're busy
        return "hello there"

    monkeypatch.setattr(agent_mod, "transcribe_pcm", slow_stt)
    task = asyncio.create_task(agent.run())
    await asyncio.sleep(1.2)
    frames = speech_frames() + [bytes(FRAME_BYTES)] * 40
    await feed_realtime(agent.frames, frames)          # utterance(s) #1
    await feed_realtime(agent.frames, frames)          # more speech while turn 1 runs
    await asyncio.sleep(3.0)
    agent.cancel()
    await asyncio.wait_for(task, 5)
    # Everything heard reached STT, and the later speech came as merged turns rather than
    # one turn per fragment.
    assert len(heard) >= 2
    assert len(heard) < 4 + 1
