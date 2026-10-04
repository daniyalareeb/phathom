"""WsPlayer: protocol encoding, ack, interrupt (fake WS client)."""

import asyncio
import json
import struct
import wave

from phathom.server.ws_audio import KIND_BOT, WsPlayer


def _wav(path, secs=0.5, rate=16000):
    n = int(rate * secs)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x10" * n)


class FakeWs:
    def __init__(self):
        self.json_msgs: list[dict] = []
        self.binary: list[bytes] = []

    async def send_json(self, msg: dict):
        self.json_msgs.append(msg)

    async def send_binary(self, raw: bytes):
        self.binary.append(raw)


async def _play_with_ack(player: WsPlayer, ws: FakeWs, wav, completed=True):
    """Drive play() while answering the play_end ack like the extension would."""
    task = asyncio.create_task(player.play(wav))
    pid = None
    for _ in range(200):
        await asyncio.sleep(0.01)
        begins = [m for m in ws.json_msgs if m.get("t") == "play_begin"]
        if begins:
            pid = begins[0]["id"]
        if any(m.get("t") == "play_end" for m in ws.json_msgs):
            break
    assert pid is not None, "no play_begin sent"
    player.on_played(pid, completed)
    return await asyncio.wait_for(task, timeout=10)


async def test_play_sends_begin_chunks_end(tmp_path):
    p = tmp_path / "say.wav"
    _wav(p, secs=0.5)
    ws = FakeWs()
    player = WsPlayer(ws.send_json, ws.send_binary)
    assert await _play_with_ack(player, ws, p) is True
    kinds = [m["t"] for m in ws.json_msgs]
    assert "play_begin" in kinds and kinds[-1] == "play_end"
    assert ws.binary, "no bot audio chunks sent"
    for raw in ws.binary:
        assert raw[0] == KIND_BOT
        assert len(raw) > 5
    seqs = [struct.unpack_from("<I", raw, 1)[0] for raw in ws.binary]
    assert seqs == sorted(seqs), "chunk seq must increase"
    for _ in range(100):  # _stream task settles just after the ack resolves play()
        if player.playing is False:
            break
        await asyncio.sleep(0.01)
    assert player.playing is False


async def test_interrupt_stops_and_returns_false(tmp_path):
    p = tmp_path / "long.wav"
    _wav(p, secs=5.0)
    ws = FakeWs()
    player = WsPlayer(ws.send_json, ws.send_binary)
    task = asyncio.create_task(player.play(p))
    await asyncio.sleep(0.3)
    assert player.playing is True
    await player.interrupt()
    assert await asyncio.wait_for(task, timeout=10) is False
    assert any(m.get("t") == "stop_audio" for m in ws.json_msgs)
    assert player.playing is False


async def test_play_records_bot_pcm(tmp_path):
    p = tmp_path / "say.wav"
    _wav(p, secs=0.3)
    ws = FakeWs()
    seen: list[bytes] = []
    player = WsPlayer(ws.send_json, ws.send_binary, on_pcm=seen.append)
    assert await _play_with_ack(player, ws, p) is True
    assert sum(len(c) for c in seen) == sum(len(b) - 5 for b in ws.binary)


def test_player_interface_matches_legacy_player():
    """agent.py takes any object with play/interrupt/playing — assert the shape."""
    import inspect
    from phathom.audio.player import Player
    for name in ("play", "interrupt", "playing"):
        assert hasattr(WsPlayer, name) and hasattr(Player, name)
    assert inspect.iscoroutinefunction(WsPlayer.play)
    assert inspect.iscoroutinefunction(WsPlayer.interrupt)
