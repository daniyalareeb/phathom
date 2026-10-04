"""B3: calls end to end — a fake extension drives the WS manager through a full
call with fixture wavs and asserts a call record with correct speakers.

Covers: ring → decide → accept flag → bot-answered listen session (disclosure
+ record + pipeline), outgoing copilot (both human sides), and graceful
disconnect (bot hangs up, copilot keeps the call).
"""

import asyncio
import base64
import wave
from pathlib import Path

import phathom.server.call_session as sess_mod
import phathom.server.ws as ws_mod
from phathom import state as state_mod
from phathom.server import runtime_settings
from phathom.server.ws import ExtensionManager

FIX = Path(__file__).parent.parent / "fixtures"


class FakeStore:
    def __init__(self):
        self.created: list[dict] = []
        self.closed = False

    async def connect(self):
        return self

    async def close(self):
        self.closed = True

    async def create_call(self, call_id, caller, started_at, mode, status="live", audio_dir=None):
        self.created.append({"call_id": call_id, "caller": caller, "mode": mode})
        return {"id": call_id}

    async def update_call(self, call_id, fields):
        return {}


def _wav_frames(path: Path, step: int = 960):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000, w.getframerate()
        data = w.readframes(w.getnframes())
    return [data[i:i + step] for i in range(0, len(data) - step + 1, step)]


def _setup_connected(monkeypatch, mode: str):
    state_mod.write({"connected": True, "mode": mode, "copilot": True,
                     "daemon_pid": None, "current_call": None})
    runtime_settings.set_many({"answer_delay_s": 0})
    store = FakeStore()
    monkeypatch.setattr(ws_mod, "Store", lambda *a, **k: store)
    ran = {}

    async def fake_pipeline(call_id, **kw):
        ran.update(call_id=call_id, kw=kw)

    monkeypatch.setattr("phathom.pipeline.run_pipeline", fake_pipeline)
    return store, ran


async def _drain(manager: ExtensionManager, secs: float = 0.3):
    await asyncio.sleep(secs)


async def test_ring_answers_and_listen_session_records(monkeypatch, tmp_path):
    """Incoming ring (mode notes) → accept after delay → listen session with
    disclosure, caller audio recorded, pipeline run."""
    tone = tmp_path / "disclosure.wav"
    import math
    import struct
    with wave.open(str(tone), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(
            struct.pack("<h", int(0.3 * 0x7FFF * math.sin(2 * math.pi * 440 * i / 48000)))
            for i in range(4800)))

    async def fake_synth(text, lang):
        return str(tone)

    monkeypatch.setattr(sess_mod, "synth", fake_synth)
    store, ran = _setup_connected(monkeypatch, "notes")
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]

    await manager.on_ring("Ahmed", False)
    assert manager.ringing is True
    # answer_delay_s = 0: the accept order goes out on the next loop tick.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if manager._expect_bot:
            break
    assert manager._expect_bot is True
    assert any(m.get("t") == "accept" for m in sent)

    # The extension answers; background rewrites answered_by to "bot" (B3).
    await manager.on_call_started("incoming", "Ahmed", "bot")
    assert manager.session is not None and manager.session.kind == "listen"
    # Ack the disclosure like the extension would.
    for _ in range(200):
        await asyncio.sleep(0.02)
        begins = [m for m in sent if m.get("t") == "play_begin"]
        if begins:
            manager.session.on_played(begins[0]["id"], True)
            break
    for frame in _wav_frames(FIX / "fix2.wav")[:40]:
        manager.session.feed("caller", frame)
    await manager.on_call_ended("ui-gone")
    assert ran["call_id"] == manager.session.call_id if manager.session else ran["call_id"]
    kw = ran["kw"]
    assert kw["mode"] == "notes" and kw["disclosure_played"] is True
    assert Path(kw["caller_wav"]).exists()
    assert store.created and store.created[0]["mode"] == "notes"


async def test_outgoing_copilot_records_both_sides(monkeypatch):
    """Owner-placed call with copilot on: caller.wav + owner.wav recorded with
    correct speakers, pipeline receives both."""
    store, ran = _setup_connected(monkeypatch, "off")
    manager = ExtensionManager()
    await manager.on_call_started("outgoing", "Ammi", "owner")
    assert manager.session is not None and manager.session.kind == "copilot"
    caller_frames = _wav_frames(FIX / "fix2.wav")[:30]
    owner_frames = _wav_frames(FIX / "fix3.wav")[:30]
    for frame in caller_frames:
        manager.session.feed("caller", frame)
    for frame in owner_frames:
        manager.session.feed("owner", frame)
    await manager.on_call_ended("ui-gone")
    kw = ran["kw"]
    assert kw["mode"] == "copilot"
    assert Path(kw["caller_wav"]).exists() and Path(kw["owner_wav"]).exists()
    assert Path(kw["caller_wav"]).stat().st_size > 44
    assert Path(kw["owner_wav"]).stat().st_size > 44
    assert store.created and store.created[0]["mode"] == "copilot"


async def test_outgoing_unknown_peer_still_copilots(monkeypatch, tmp_path):
    """Outgoing peer name is unverified → 'Unknown'; a block rule on 'Unknown'
    (for unknown callers) must not stop Copilot on the owner's own call."""
    from phathom import contacts as contacts_mod
    store, ran = _setup_connected(monkeypatch, "off")
    monkeypatch.setattr(contacts_mod, "load", lambda: {"policy": "all", "block": ["Unknown", "Spammer"], "allow": [], "notes": {}})
    manager = ExtensionManager()
    await manager.on_call_started("outgoing", "Unknown", "owner")
    assert manager.session is not None and manager.session.kind == "copilot"
    manager.session.close_ingest("test")
    manager.session = None
    # A contact blocked by real name is still never recorded.
    await manager.on_call_started("outgoing", "Spammer", "owner")
    assert manager.session is None


async def test_blocked_contact_not_answered(monkeypatch):
    from phathom import contacts as contacts_mod
    store, ran = _setup_connected(monkeypatch, "notes")
    monkeypatch.setattr(contacts_mod, "should_answer", lambda *a, **k: False)
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]
    await manager.on_ring("Spammer", False)
    await _drain(manager)
    assert manager._expect_bot is False
    assert not [m for m in sent if m.get("t") == "accept"]


async def test_let_it_ring_cancels_bot_answer(monkeypatch):
    """§7.3: side panel 'Let it ring' (ui_action ignore) stops the auto-answer."""
    store, ran = _setup_connected(monkeypatch, "notes")
    runtime_settings.set_many({"answer_delay_s": 0.3})
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]
    await manager.on_ring("Ahmed", False)
    await manager.on_json({"t": "ui_action", "action": "ignore"})
    await _drain(manager, 0.6)
    assert manager._expect_bot is False
    assert not [m for m in sent if m.get("t") == "accept"]


async def test_answer_now_is_attributed_to_bot(monkeypatch):
    """§7.3: side panel 'Answer now (bot)' accepts via the server → bot session."""
    store, ran = _setup_connected(monkeypatch, "notes")
    runtime_settings.set_many({"answer_delay_s": 30})
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]
    await manager.on_ring("Ahmed", False)
    await manager.on_json({"t": "ui_action", "action": "answer_now"})
    assert [m for m in sent if m.get("t") == "accept"]
    await manager.on_call_started("incoming", "Ahmed", "owner")
    assert manager.session is not None and manager.session.kind == "listen"
    manager.session.close_ingest("test")


async def test_hooks_failed_disables_answer_and_copilot(monkeypatch):
    """§10: hello/hooks with gum=false → no auto-answer, no Copilot session."""
    store, ran = _setup_connected(monkeypatch, "notes")
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]
    await manager.on_json({"t": "hooks", "hooks": {"gum": False, "pc": True}})
    await manager.on_ring("Ahmed", False)
    await _drain(manager)
    assert manager._expect_bot is False
    assert not [m for m in sent if m.get("t") == "accept"]
    await manager.on_call_started("outgoing", "Ammi", "owner")
    assert manager.session is None
    # Hooks recover → Copilot works again.
    await manager.on_json({"t": "hooks", "hooks": {"gum": True, "pc": True}})
    await manager.on_call_started("outgoing", "Ammi", "owner")
    assert manager.session is not None and manager.session.kind == "copilot"
    manager.session.close_ingest("test")


async def test_disconnect_graceful_bot_vs_copilot(monkeypatch):
    store, ran = _setup_connected(monkeypatch, "notes")
    manager = ExtensionManager()
    sent: list[dict] = []

    async def fake_send(msg: dict):
        sent.append(msg)

    manager.send_json = fake_send  # type: ignore[method-assign]
    # Bot call: disconnect sends hangup and closes ingest.
    await manager.on_call_started("incoming", "Ahmed", "bot")
    assert manager.session is not None
    await manager.disconnect_gracefully()
    assert any(m.get("t") == "hangup" for m in sent)
    assert manager.session.open is False
    await manager._session_task
    # Copilot call: disconnect closes ingest WITHOUT hangup (call continues).
    sent.clear()
    await manager.on_call_started("outgoing", "Ammi", "owner")
    assert manager.session is not None and manager.session.kind == "copilot"
    await manager.disconnect_gracefully()
    assert not [m for m in sent if m.get("t") == "hangup"]
    await manager._session_task


def test_json_audio_tab_track(tmp_path):
    """Tab-capture JSON audio keeps its label and lands in tab.wav."""
    import asyncio as _a

    async def go():
        manager = ExtensionManager()
        from phathom.server.call_session import CallSession
        events: list[dict] = []

        async def publish(e):
            events.append(e)

        async def noop(msg):
            pass

        session = CallSession(call_id="t9", peer="X", direction="incoming", kind="copilot",
                              started_at="2026-10-03T00:00:00+00:00",
                              audio_dir=tmp_path / "rec",
                              send_json=noop, send_binary=noop, publish=publish,
                              store_factory=lambda: _a.sleep(0, result=None),
                              live_transcript=False)
        manager.session = session
        pcm = base64.b64encode(b"\x07\x08" * 1600).decode()
        manager.on_json_audio({"t": "audio", "track": "tab", "pcmBase64": pcm})
        assert session.tab_seen is True
        session.close_ingest("test")
        return tmp_path / "rec" / "tab.wav"

    path = _a.run(go())
    assert path.exists()
