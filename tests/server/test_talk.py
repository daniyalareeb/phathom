"""B4: talk mode — muted WsPlayer, take-over gating, talk→listen fallback."""

import asyncio

import phathom.server.ws as ws_mod
from phathom import state as state_mod
from phathom.server import runtime_settings
from phathom.server.call_session import CallSession
from phathom.server.ws import ExtensionManager
from phathom.server.ws_audio import WsPlayer


async def _player():
    sent_j: list[dict] = []
    sent_b: list[bytes] = []

    async def send_json(msg: dict):
        sent_j.append(msg)

    async def send_binary(raw: bytes):
        sent_b.append(raw)

    return WsPlayer(send_json, send_binary), sent_j, sent_b


async def test_muted_player_sends_nothing(tmp_path):
    import wave
    wav = tmp_path / "t.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 4800)
    player, sent_j, sent_b = await _player()
    player.muted = True
    assert await player.play(wav) is False
    assert sent_j == [] and sent_b == []


def _session(tmp_path):
    async def noop(msg):
        pass

    async def publish(e):
        pass

    async def nostore():
        return None

    return CallSession(call_id="tk", peer="Ahmed", direction="incoming", kind="talk",
                       started_at="2026-10-03T00:00:00+00:00", audio_dir=tmp_path / "rec",
                       send_json=noop, send_binary=noop, publish=publish,
                       store_factory=nostore, live_transcript=False)


async def test_take_over_mutes_and_starves_agent_but_records(tmp_path):
    s = _session(tmp_path)
    s.feed("caller", b"\x01\x02" * 480)
    assert s.agent_queue.qsize() >= 0
    # drain
    while not s.agent_queue.empty():
        s.agent_queue.get_nowait()
    await s.take_over()
    assert s.taken_over is True and s._player.muted is True
    s.feed("caller", b"\x01\x02" * 480)
    assert s.agent_queue.empty()  # agent stops listening
    await s.hand_back()
    assert s.taken_over is False and s._player.muted is False
    s.feed("caller", b"\x01\x02" * 480)
    assert not s.agent_queue.empty()  # listening resumes
    s.close_ingest("test")


async def test_talk_falls_back_to_listen_when_disabled(monkeypatch):
    state_mod.write({"connected": True, "mode": "assistant", "copilot": True,
                     "daemon_pid": None, "current_call": None})
    runtime_settings.set_many({"talk_mode_enabled": False})
    try:
        from phathom.server.ws import ExtensionManager as EM
        manager = EM()
        # stub session start to capture kind without running audio
        kinds: list[str] = []
        orig_start = manager._start_session

        async def spy(kind, peer, direction):
            kinds.append(kind)
            # do not start a real session; emulate enough for the test
            manager._expect_bot = False

        monkeypatch.setattr(manager, "_start_session", spy)
        await manager.on_call_started("incoming", "Ahmed", "bot")
        assert kinds == ["listen"]
        assert orig_start is not None
    finally:
        runtime_settings.set_many({"talk_mode_enabled": True})


async def test_talk_allowed_by_default(monkeypatch):
    state_mod.write({"connected": True, "mode": "assistant", "copilot": True,
                     "daemon_pid": None, "current_call": None})
    manager = ExtensionManager()
    kinds: list[str] = []

    async def spy(kind, peer, direction):
        kinds.append(kind)

    monkeypatch.setattr(manager, "_start_session", spy)
    await manager.on_call_started("incoming", "Ahmed", "bot")
    assert kinds == ["talk"]
    _ = ws_mod  # keep import used
    await asyncio.sleep(0)
