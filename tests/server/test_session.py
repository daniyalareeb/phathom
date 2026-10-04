"""CallSession: recording, bot playback, live transcript, pipeline handoff (all faked I/O)."""

import asyncio
import wave
from pathlib import Path

import phathom.server.call_session as sess_mod
from phathom.server.call_session import CallSession

FIX = Path(__file__).parent.parent / "fixtures"


class Send:
    def __init__(self):
        self.json_msgs: list[dict] = []
        self.binary: list[bytes] = []

    async def send_json(self, msg: dict):
        self.json_msgs.append(msg)

    async def send_binary(self, raw: bytes):
        self.binary.append(raw)


class FakeStore:
    def __init__(self):
        self.created = []
        self.updated = []

    async def create_call(self, call_id, caller, started_at, mode, status="live", audio_dir=None):
        self.created.append({"call_id": call_id, "mode": mode})
        return {"id": call_id}

    async def update_call(self, call_id, fields):
        self.updated.append(fields)
        return {}

    async def close(self):
        pass


def _sine_wav(path: Path, secs=0.6, rate=48000):
    import math
    import struct
    n = int(rate * secs)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(0.4 * 0x7FFF * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(n)))


def _make(kind, tmp_path, live_transcript=False, published=None):
    send = Send()
    store = FakeStore()
    events = published if published is not None else []

    async def publish(event):
        events.append(event)

    session = CallSession(call_id="t1", peer="Ahmed", direction="incoming", kind=kind,
                          started_at="2026-10-03T00:00:00+00:00",
                          audio_dir=tmp_path / "rec",
                          send_json=send.send_json, send_binary=send.send_binary,
                          publish=publish,
                          store_factory=lambda: _connect(store),
                          live_transcript=live_transcript)
    return session, send, store, events


async def _connect(store):
    return store


async def test_copilot_records_three_tracks_and_pipelines(tmp_path, monkeypatch):
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran.update(call_id=call_id, kw=kw)

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    session, send, store, published = _make("copilot", tmp_path)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    session.feed("caller", b"\x01\x02" * 480)
    session.feed("owner", b"\x03\x04" * 480)
    session.feed("speaker", b"\x05\x06" * 480)
    await asyncio.sleep(0.1)
    session.close_ingest("test-end")
    result = await asyncio.wait_for(task, timeout=30)
    assert result["kind"] == "copilot" and result["reason"] == "test-end"
    for t in ("caller.wav", "owner.wav", "speaker.wav"):
        assert (tmp_path / "rec" / t).exists()
    assert store.created and store.created[0]["mode"] == "copilot"
    assert ran["call_id"] == "t1" and ran["kw"]["mode"] == "copilot"
    assert ran["kw"]["owner_wav"].name == "owner.wav"
    types = [e["event"]["type"] for e in published if e.get("t") == "live"]
    assert {"call_started", "call_ended", "summary_ready"} <= set(types)


async def test_listen_plays_disclosure_then_records(tmp_path, monkeypatch):
    tone = tmp_path / "disclosure.wav"
    _sine_wav(tone)

    async def fake_synth(text, lang):
        return str(tone)

    monkeypatch.setattr(sess_mod, "synth", fake_synth)
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran["kw"] = kw

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    session, send, store, published = _make("listen", tmp_path)
    task = asyncio.create_task(session.run())
    # ack the disclosure playback like the extension would
    pid = None
    for _ in range(300):
        await asyncio.sleep(0.02)
        begins = [m for m in send.json_msgs if m.get("t") == "play_begin"]
        if begins:
            pid = begins[0]["id"]
            break
    assert pid is not None, "disclosure never played"
    session.on_played(pid, True)
    await asyncio.sleep(0.2)
    session.feed("caller", b"\x01\x02" * 480)
    session.close_ingest("test-end")
    await asyncio.wait_for(task, timeout=30)
    assert (tmp_path / "rec" / "bot.wav").exists()  # disclosure recorded server-side
    assert ran["kw"]["disclosure_played"] is True
    gains = [m for m in send.json_msgs if m.get("t") == "set_gain"]
    assert gains and gains[0] == {"t": "set_gain", "mic": 0, "bot": 1}


async def test_live_transcript_publishes_speech(tmp_path, monkeypatch):
    async def fake_stt(pcm, model, prompt=None):
        return "hello from the caller"

    monkeypatch.setattr(sess_mod, "transcribe_pcm", fake_stt)
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran["done"] = True

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    published: list[dict] = []
    session, send, store, _ = _make("copilot", tmp_path, live_transcript=True,
                                    published=published)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    with wave.open(str(FIX / "fix2.wav"), "rb") as w:
        data = w.readframes(w.getnframes())
    for i in range(0, len(data) - 960 + 1, 960):
        session.feed("caller", data[i:i + 960])
    for _ in range(40):  # trailing silence closes the utterance
        session.feed("caller", bytes(960))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if any(e.get("event", {}).get("type") == "transcript" for e in published):
            break
    session.close_ingest("test-end")
    await asyncio.wait_for(task, timeout=30)
    transcripts = [e["event"] for e in published
                   if e.get("event", {}).get("type") == "transcript"]
    assert transcripts and transcripts[0]["speaker"] == "caller"
    assert "hello from the caller" in transcripts[0]["text"]


async def test_speaker_tap_labels_get_own_wavs(tmp_path, monkeypatch):
    """STEP 1A: each speaker:N tap records its own wav for diagnosis."""
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran["done"] = True

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    session, send, store, published = _make("copilot", tmp_path)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    session.feed("speaker:1(AudioWorkletNode)", b"\x01\x02" * 480)
    session.feed("speaker:2(MediaStreamAudioSourceNode)", b"\x03\x04" * 480)
    session.feed("speaker", b"\x05\x06" * 480)
    session.close_ingest("test-end")
    await asyncio.wait_for(task, timeout=30)
    rec = tmp_path / "rec"
    assert (rec / "taps" / "speaker_1_audioworkletnode.wav").exists()
    assert (rec / "taps" / "speaker_2_mediastreamaudiosourcenode.wav").exists()
    assert (rec / "speaker.wav").exists()
    assert ran.get("done") is True


async def test_json_audio_keeps_speaker_label(tmp_path, monkeypatch):
    """ws manager passes full speaker:N labels through to the session."""
    import base64

    from phathom.server.ws import ExtensionManager
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran["done"] = True

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    manager = ExtensionManager()
    session, send, store, published = _make("copilot", tmp_path)

    manager.session = session
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    pcm = base64.b64encode(b"\x07\x08" * 480).decode()
    manager.on_json_audio({"t": "audio", "track": "speaker:3(X)", "pcmBase64": pcm})
    session.close_ingest("test-end")
    await asyncio.wait_for(task, timeout=30)
    assert (tmp_path / "rec" / "taps" / "speaker_3_x.wav").exists()
