"""Minimal Phase-0 spike server for the Chrome extension.

- FastAPI on 127.0.0.1:8765 ONLY. No auth (spike only).
- WS /ws: receives JSON events + binary audio frames:
    binary = [1 byte kind][4 bytes uint32 LE seq][PCM s16le mono 16 kHz]
    kind 0x01 = caller, 0x02 = owner.
  Also accepts JSON audio {t:"audio", track, pcmBase64} for robustness.
- Writes data/spike/<timestamp_peer>/caller.wav + owner.wav (+ events.log).
- Saves DOM dumps to data/dumps/<ts>_<label>.html.
- Logs every event.
"""
from __future__ import annotations

import base64
import json
import logging
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("phathom.spike")

SAMPLE_RATE = 16000


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _slug(s: str) -> str:
    out = "".join(c.lower() if (c.isalnum()) else "_" for c in (s or ""))
    out = "_".join(p for p in out.split("_") if p)
    return out[:32] or "unknown"


def _wav_name(track: str) -> str:
    """Fixed names for the classic tracks; one sanitized wav per speaker:N tap label."""
    if track == "owner" or track.startswith("owner"):
        return "owner.wav"
    if track == "caller" or track.startswith("caller"):
        return "caller.wav"
    if track == "speaker":
        return "speaker.wav"
    safe = "".join(c.lower() if c.isalnum() else "_" for c in track).strip("_")[:48]
    return (safe or "unknown") + ".wav"


class CallWavs:
    """Streaming wav files (caller + owner + one per speaker:N tap label), 16 kHz s16le mono."""

    def __init__(self, call_dir: Path):
        self.dir = call_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._wavs: dict[str, wave.Wave_write] = {}
        self._paths: dict[str, Path] = {}
        self._bytes: dict[str, int] = {}
        self.events_path = self.dir / "events.log"
        log.info("spike call dir: %s", self.dir)

    def _sink(self, track: str) -> wave.Wave_write:
        name = _wav_name(track)
        if name not in self._wavs:
            path = self.dir / name
            w = wave.open(str(path), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            self._wavs[name] = w
            self._paths[name] = path
            self._bytes[name] = 0
            log.info("spike new track %r -> %s", track, path)
        return self._wavs[name]

    def write(self, track: str, pcm: bytes) -> None:
        if not pcm:
            return
        name = _wav_name(track)
        self._sink(track).writeframes(pcm)
        self._bytes[name] += len(pcm)

    def track_seconds(self, track: str) -> float:
        return self._bytes.get(_wav_name(track), 0) / 2 / SAMPLE_RATE

    def log_event(self, line: str) -> None:
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")

    def durations(self) -> tuple[float, float]:
        return (self.track_seconds("caller"), self.track_seconds("owner"))

    def summary(self) -> str:
        parts = [f"{name}={self._bytes[name] / 2 / SAMPLE_RATE:.1f}s"
                 for name in sorted(self._bytes)]
        return ", ".join(parts)

    def close(self) -> None:
        for w in self._wavs.values():
            try:
                w.close()
            except Exception:
                pass
        log.info("closed %s %s", self.dir, self.summary())


def create_app() -> FastAPI:
    app = FastAPI(title="phathom-spike")
    Path("data/spike").mkdir(parents=True, exist_ok=True)
    Path("data/dumps").mkdir(parents=True, exist_ok=True)

    @app.get("/health")
    async def health():
        return {"server": "spike", "ok": True}

    @app.post("/api/dev/dump")
    async def dev_dump(payload: dict):
        html = payload.get("html", "")
        label = _slug(payload.get("label", "manual"))
        path = Path("data/dumps") / f"{_ts()}_{label}.html"
        path.write_text(html, encoding="utf-8")
        log.info("DOM dump (%d chars) -> %s", len(html), path)
        return {"ok": True, "path": str(path), "chars": len(html)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        session_ts = _ts()
        wavs: CallWavs | None = None
        peer = "precall"

        def ensure_call_dir(caller: str) -> CallWavs:
            nonlocal wavs, peer
            peer = caller
            # New dir per call so evidence stays separated.
            call_dir = Path("data/spike") / f"{session_ts}_{_slug(caller)}"
            i = 1
            base = call_dir
            while call_dir.exists():
                i += 1
                call_dir = Path(str(base) + f"_{i}")
            if wavs is not None:
                try:
                    wavs.close()
                except Exception:
                    pass
            wavs = CallWavs(call_dir)
            return wavs

        # Pre-call dir so early audio/frames are not lost.
        wavs = CallWavs(Path("data/spike") / f"{session_ts}_precall")
        wavs.log_event("ws_connected")
        log.info("WS connected -> %s", wavs.dir)
        await ws.send_text(json.dumps({"t": "welcome", "state": {"connected": True}}))

        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    try:
                        data = json.loads(msg["text"])
                    except Exception:
                        log.warning("non-JSON text frame (%d chars)", len(msg["text"]))
                        continue
                    t = data.get("t", "?")
                    if t in ("ping", "pong", "hello"):
                        if t == "ping":
                            await ws.send_text(json.dumps({"t": "pong"}))
                        wavs.log_event(f"event {t} {json.dumps(data)[:300]}")
                        log.info("event %-12s %s", t, json.dumps(data)[:300])
                        continue
                    if t == "audio" and data.get("pcmBase64"):
                        try:
                            pcm = base64.b64decode(data["pcmBase64"])
                            wavs.write(data.get("track", "caller"), pcm)
                        except Exception as e:
                            log.warning("bad audio json: %r", e)
                        continue
                    if t == "dom_dump":
                        html = data.get("html", "")
                        label = _slug(data.get("label", "manual"))
                        path = Path("data/dumps") / f"{_ts()}_{label}.html"
                        path.write_text(html, encoding="utf-8")
                        wavs.log_event(f"dom_dump {len(html)} chars -> {path}")
                        log.info("DOM dump (%d chars, href=%s) -> %s",
                                 len(html), data.get("href", "?"), path)
                        await ws.send_text(json.dumps(
                            {"t": "dump_saved", "path": str(path), "chars": len(html)}))
                        continue
                    # Call/event logging; rotate dir on call_started.
                    if t == "call_started":
                        wavs.log_event(f"event {t} {json.dumps(data)}")
                        log.info("event call_started direction=%s peer=%s answered_by=%s",
                                 data.get("direction"), data.get("peer"), data.get("answered_by"))
                        ensure_call_dir(str(data.get("peer", "unknown")))
                        wavs.log_event(f"event {t} {json.dumps(data)}")
                        await ws.send_text(json.dumps({"t": "ack", "ack": "call_started"}))
                        continue
                    wavs.log_event(f"event {t} {json.dumps(data)[:500]}")
                    log.info("event %-12s %s", t, json.dumps(data)[:500])
                elif "bytes" in msg and msg["bytes"] is not None:
                    raw: bytes = msg["bytes"]
                    if len(raw) < 5:
                        log.warning("short binary frame (%d bytes)", len(raw))
                        continue
                    kind = raw[0]
                    (seq,) = struct.unpack_from("<I", raw, 1)
                    pcm = raw[5:]
                    track = {0x02: "owner", 0x03: "speaker"}.get(kind, "caller")
                    try:
                        wavs.write(track, pcm)
                    except Exception as e:
                        log.warning("wav write failed: %r", e)
                else:
                    log.warning("unknown WS message keys: %s", list(msg.keys()))
        except WebSocketDisconnect:
            log.info("WS disconnected")
        except Exception as e:  # noqa: BLE001 - keep spike server alive on any client error
            log.info("WS closed with error: %r", e)
        finally:
            try:
                if wavs is not None:
                    cd, od = wavs.durations()
                    wavs.log_event(f"ws_closed caller_s={cd:.1f} owner_s={od:.1f}")
                    wavs.close()
            except Exception:
                pass

    return app


app = create_app()


def run(port: int = 8765) -> None:
    import uvicorn
    # Bind ONLY to loopback for the spike (no auth).
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
