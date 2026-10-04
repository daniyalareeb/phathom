"""Extension WebSocket endpoint + connection manager (EXTENSION_SPEC §6.1, §6.2).

Text frames are JSON ``{"t": ...}``; binary frames are
``[kind][seq u32 LE][PCM]`` (kinds in ws_audio). One manager per server process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from phathom import contacts as contacts_mod
from phathom import state as state_mod
from phathom.config import settings
from phathom.server import runtime_settings
from phathom.server.auth import verify_token
from phathom.server.call_session import ANSWER_OF, CallSession, decide, now_iso
from phathom.server.events import Hub
from phathom.server.ws_audio import KIND_BOT, KIND_CALLER, KIND_OWNER, KIND_SPEAKER, KIND_TAB
from phathom.store import Store, make_call_id

log = logging.getLogger("phathom.server.ws")

TRACK_OF_KIND = {KIND_CALLER: "caller", KIND_OWNER: "owner",
                 KIND_SPEAKER: "speaker", KIND_TAB: "tab"}
RECONNECT_GRACE_S = 60


class ExtensionManager:
    def __init__(self) -> None:
        self.hub = Hub()
        self.ws: WebSocket | None = None
        self.ext_version: str | None = None
        self.wa_logged_in: bool | None = None
        self.hooks_ok: dict = {}
        # ring bookkeeping
        self.ringing = False
        self.ring_caller: str | None = None
        self.ring_is_video = False
        self._answer_task: asyncio.Task | None = None
        self._expect_bot = False
        # active call
        self.session: CallSession | None = None
        self._session_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None
        # extension diagnostics (§7.5): last reported hook/worklet/rms state
        self.ext_hooks: dict = {}
        self.worklet_mode: str = "unknown"
        self.tap_rms: dict[str, float] = {}  # tap label -> last dBFS
        self.outgoing_peer_unverified: bool = False
        self.selector_check: list | None = None  # live, from the WhatsApp tab
        self.selector_at: float = 0.0
        self.tab_seen_at: float = 0.0  # last message from the top WhatsApp frame
        self.last_tone: str | None = None

    # ---- outbound ----

    async def send_json(self, msg: dict) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001
            pass

    async def send_binary(self, raw: bytes) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send_bytes(raw)
        except Exception:  # noqa: BLE001
            pass

    async def publish(self, event: dict) -> None:
        await self.hub.publish(event)
        # UI pages receive live events through the extension (server → ext → UI).
        if event.get("t") == "live":
            await self.send_json(event)

    def snapshot(self) -> dict:
        st = state_mod.read()
        return {"connected": bool(st.get("connected", False)), "mode": st.get("mode", "assistant"),
                "copilot": bool(st.get("copilot", True)),
                "answer_delay_s": runtime_settings.get("answer_delay_s"),
                "live_transcript": runtime_settings.get("live_transcript"),
                "side_panel_auto": runtime_settings.get("side_panel_auto"),
                "wa_logged_in": self.wa_logged_in,
                "in_call": self.session.open if self.session else False}

    # ---- event ingress ----

    async def on_json(self, data: dict) -> None:
        t = data.get("t", "?")
        if data.get("_isTop"):
            self.tab_seen_at = time.time()
        if t == "selectors":
            self.selector_check = data.get("items") or []
            self.selector_at = time.time()
            return
        if t == "ping":
            await self.send_json({"t": "pong"})
        elif t == "hello":
            self.ext_version = data.get("ext_version")
            await self.send_json({"t": "welcome", "state": self.snapshot()})
        elif t == "wa_status":
            self.wa_logged_in = bool(data.get("logged_in"))
            state_mod.write({"wa_logged_in": self.wa_logged_in})
        elif t == "hooks":
            self.hooks_ok = data.get("hooks", {}) or {}
            self.ext_hooks = dict(self.hooks_ok)
            if data.get("worklet"):
                self.worklet_mode = str(data["worklet"])
        elif t == "diag":
            self._on_diag(data)
        elif t == "ring":
            await self.on_ring(str(data.get("caller", "Unknown")),
                               bool(data.get("is_video", False)))
        elif t == "ring_gone":
            await self.on_ring_gone()
        elif t == "call_started":
            await self.on_call_started(str(data.get("direction", "incoming")),
                                       str(data.get("peer", data.get("caller", "Unknown"))),
                                       str(data.get("answered_by", "owner")))
        elif t == "call_ended":
            await self.on_call_ended(str(data.get("reason", "ui-gone")))
        elif t == "played":
            if self.session:
                self.session.on_played(int(data.get("id", 0)), bool(data.get("completed", True)))
        elif t == "ui_action":
            await self.on_ui_action(data)
        elif t in ("tone_started", "tone_ended", "dom_dump", "pong"):
            if t in ("tone_started", "tone_ended"):
                self.last_tone = t
            log.info("ext event %-12s %s", t, json.dumps(data)[:300])
        elif t == "audio" and data.get("pcmBase64"):
            # JSON audio relay (speaker:N taps keep labels; binary has no label).
            self.on_json_audio(data)
        else:
            log.info("ext event %-12s %s", t, json.dumps(data)[:300])

    def _on_diag(self, data: dict) -> None:
        key = str(data.get("key", ""))
        payload = data.get("data")
        if key == "rms" and isinstance(payload, dict) and "label" in payload:
            try:
                self.tap_rms[str(payload["label"])] = float(payload.get("db", -999))
            except (TypeError, ValueError):
                pass
        elif key == "outgoing_peer_unverified":
            self.outgoing_peer_unverified = True
            log.warning("outgoing peer name is UNVERIFIED (needs DOM dump): %r", payload)
        else:
            log.info("ext diag %-24s %s", key, json.dumps(payload)[:200])

    def on_binary(self, raw: bytes) -> None:
        if len(raw) < 5:
            return
        kind = raw[0]
        track = TRACK_OF_KIND.get(kind)
        if track is None or self.session is None or not self.session.open:
            return
        self.session.feed(track, raw[5:])

    def on_json_audio(self, data: dict) -> None:
        """Fallback path: JSON {t:'audio', track, pcmBase64} (spike-era robustness).

        STEP 1A: speaker:N labels pass through untouched so each tap gets its own wav.
        """
        if self.session is None or not self.session.open:
            return
        try:
            pcm = base64.b64decode(data.get("pcmBase64", ""))
        except Exception:  # noqa: BLE001
            return
        track = str(data.get("track", "caller"))
        if track.startswith("owner"):
            key = "owner"
        elif track.startswith("tab"):
            key = "tab"  # §5.7 source 3 keeps its label (own wav + pick input)
        elif track.startswith("speaker"):
            key = track  # keep the full speaker:N(label) for per-tap wavs
        else:
            key = "caller"
        self.session.feed(key, pcm)

    # ---- call flow ----

    def hooks_failed(self) -> bool:
        """§10: the extension reported its hooks and the getUserMedia hook is
        down (no owner mic, no bot voice). Unknown (not yet reported) is not a
        failure, so a late `hooks` message never blocks a call."""
        return bool(self.hooks_ok) and not self.hooks_ok.get("gum")

    async def on_ring(self, caller: str, is_video: bool) -> None:
        self.ringing = True
        self.ring_caller = caller
        self.ring_is_video = is_video
        st = state_mod.read()
        st_mode = st.get("mode", "assistant")
        action = decide("ring", connected=bool(st.get("connected", False)),
                        answer_mode=ANSWER_OF.get(st_mode, "off"),
                        copilot=bool(st.get("copilot", True)),
                        blocked=not contacts_mod.should_answer(caller, st_mode))
        log.info("ring from %s (video=%s) -> %s", caller, is_video, action)
        await self.publish({"t": "live", "call_id": None,
                            "event": {"type": "ring", "caller": caller, "is_video": is_video}})
        if action == "answer_after_delay" and self.hooks_failed():
            log.warning("hooks failed (%s); NOT auto-answering %s", self.hooks_ok, caller)
            return
        if action == "answer_after_delay":
            self._cancel_answer()
            self._answer_task = asyncio.create_task(self._answer_after_delay(caller))

    def _cancel_answer(self) -> None:
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = None

    async def _answer_after_delay(self, caller: str) -> None:
        try:
            await asyncio.sleep(float(runtime_settings.get("answer_delay_s")))
        except asyncio.CancelledError:
            return
        if not self.ringing or self.ring_caller != caller:
            return
        st = state_mod.read()
        if not st.get("connected", False):
            return
        if ANSWER_OF.get(st.get("mode", "assistant"), "off") == "off":
            return
        log.info("answer delay elapsed; accepting call from %s", caller)
        self._expect_bot = True
        await self.send_json({"t": "accept"})

    async def on_ring_gone(self) -> None:
        self.ringing = False
        self._cancel_answer()

    async def on_call_started(self, direction: str, peer: str, answered_by: str) -> None:
        self._cancel_answer()
        self.ringing = False
        st = state_mod.read()
        contacts = contacts_mod.load()
        blocked = contacts_mod._match(peer, contacts.get("block", []))
        if direction == "incoming" and (answered_by == "bot" or self._expect_bot):
            self._expect_bot = False
            answer = ANSWER_OF.get(st.get("mode", "assistant"), "off")
            kind = "talk" if answer == "talk" else "listen"
            if kind == "talk" and not runtime_settings.get("talk_mode_enabled"):
                # B4: talk disabled by setting (e.g. bot path never verified) —
                # fall back to listen loudly instead of staying silent.
                log.warning("talk requested but talk_mode_enabled=false; falling back to listen")
                kind = "listen"
            if blocked:
                log.warning("bot answered a blocked caller (%s); hanging up", peer)
                await self.send_json({"t": "hangup"})
                return
        else:
            event = "outgoing" if direction == "outgoing" else "owner_answered"
            # Copilot = a call YOU took or placed. "Unknown" is our placeholder when
            # the name can't be read (always, on outgoing calls, until the selector
            # is verified); it must not match a block rule meant for unknown callers.
            if peer.strip().lower() == "unknown":
                blocked = False
            action = decide(event, connected=bool(st.get("connected", False)),
                            answer_mode=ANSWER_OF.get(st.get("mode", "assistant"), "off"),
                            copilot=bool(st.get("copilot", True)), blocked=blocked)
            log.info("call_started %s peer=%s answered_by=%s -> %s", direction, peer, answered_by, action)
            if action != "record_copilot":
                return
            if self.hooks_failed():
                log.warning("hooks failed (%s); Copilot disabled for this call", self.hooks_ok)
                return
            kind = "copilot"
        await self._start_session(kind, peer, direction)

    async def _start_session(self, kind: str, peer: str, direction: str) -> None:
        if self.session is not None and self.session.open:
            log.warning("second call while one is active; ignoring")
            return
        call_id = make_call_id(peer)
        audio_dir = Path("data/recordings") / call_id
        audio_dir.mkdir(parents=True, exist_ok=True)
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
        session = CallSession(call_id=call_id, peer=peer, direction=direction, kind=kind,
                              started_at=now_iso(), audio_dir=audio_dir,
                              send_json=self.send_json, send_binary=self.send_binary,
                              publish=self.publish,
                              store_factory=lambda: Store().connect(),
                              live_transcript=bool(runtime_settings.get("live_transcript")))
        self.session = session
        try:
            state_mod.write({"current_call": {"id": call_id, "caller": peer,
                                              "since": session.started_at, "kind": kind}})
        except Exception:  # noqa: BLE001
            pass
        self._session_task = asyncio.create_task(self._run_session(session))

    async def _run_session(self, session: CallSession) -> None:
        try:
            result = await session.run()
            log.info("session %s finished: %r", session.call_id, result)
        except Exception as e:  # noqa: BLE001
            log.exception("session failed: %r", e)
        finally:
            if self.session is session:
                self.session = None
            try:
                state_mod.write({"current_call": None})
            except Exception:  # noqa: BLE001
                pass

    async def disconnect_gracefully(self) -> None:
        """Connect switch turned off mid-call (B3): bot-answered calls end
        gracefully (hangup order + pipeline still runs); the owner's own
        Copilot recording just stops — the call itself continues."""
        self._cancel_answer()
        session = self.session
        if session is None or not session.open:
            return
        if session.kind == "copilot":
            log.info("disconnect during copilot call: stopping recording, call continues")
            session.close_ingest("disconnect")
        else:
            log.info("disconnect during bot call: ending gracefully")
            await session.hangup()
            session.close_ingest("disconnect")

    async def on_call_ended(self, reason: str) -> None:
        if self.session is not None and self.session.open:
            self.session.close_ingest(reason)
            if self._session_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self._session_task), timeout=180)
                except asyncio.TimeoutError:
                    log.warning("session finalize timed out")
        await self.publish({"t": "live", "call_id": None,
                            "event": {"type": "idle", "reason": reason}})

    async def on_ui_action(self, data: dict) -> None:
        action = data.get("action")
        if action == "ignore":
            # §7.3 "Let it ring": cancel the pending bot answer for this ring.
            if self.ringing:
                log.info("owner chose to let %s ring; not answering", self.ring_caller)
                self._cancel_answer()
            return
        if action == "answer_now":
            # §7.3 "Answer now (bot)": same path as the delayed auto-answer, so
            # call_started is attributed to the bot (listen/talk, not copilot).
            if self.ringing and not self.hooks_failed():
                self._cancel_answer()
                self._expect_bot = True
                await self.send_json({"t": "accept"})
            return
        if self.session is None or not self.session.open:
            return
        if action == "hangup":
            await self.session.hangup()
        elif action == "take_over":
            await self.session.take_over()
        elif action == "hand_back":
            await self.session.hand_back()
        elif action == "note":
            text = str(data.get("text", "")).strip()
            if text:
                await self._save_note(self.session, text)

    async def _save_note(self, session: CallSession, text: str) -> None:
        entry = f"[{session._elapsed():.0f}s] {text}"
        try:
            store = await Store().connect()
            try:
                call = await store.get_call(session.call_id)
                prev = (call or {}).get("notes") or ""
                await store.update_call(session.call_id,
                                        {"notes": (prev + "\n" + entry).strip()})
            finally:
                await store.close()
        except Exception as e:  # noqa: BLE001
            log.warning("note save failed: %r", e)
        await self.publish({"t": "live", "call_id": session.call_id,
                            "event": {"type": "note_saved", "text": text}})

    # ---- WS lifecycle ----

    async def on_disconnect(self) -> None:
        self.ws = None
        if self.session is not None and self.session.open:
            log.info("WS dropped mid-call; 60s grace for reconnect")
            if self._grace_task and not self._grace_task.done():
                self._grace_task.cancel()
            self._grace_task = asyncio.create_task(self._reconnect_grace())

    async def _reconnect_grace(self) -> None:
        try:
            await asyncio.sleep(RECONNECT_GRACE_S)
        except asyncio.CancelledError:
            return
        if self.session is not None and self.session.open:
            log.warning("no reconnect in 60s; finalizing with silence padding")
            self.session.close_ingest("ws-timeout")


async def websocket_endpoint(ws: WebSocket, manager: ExtensionManager) -> None:
    token = ws.query_params.get("token", "")
    if not verify_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    if manager.session is not None and manager.session.open:
        log.info("extension reconnected mid-call; resuming feed")
        if manager._grace_task and not manager._grace_task.done():
            manager._grace_task.cancel()
    manager.ws = ws
    log.info("extension WS connected (v%s)", manager.ext_version)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except ValueError:
                    continue
                if data.get("t") == "audio" and data.get("pcmBase64"):
                    manager.on_json_audio(data)
                    continue
                await manager.on_json(data)
            elif "bytes" in msg and msg["bytes"] is not None:
                manager.on_binary(msg["bytes"])
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("WS closed with error: %r", e)
    finally:
        if manager.ws is ws:
            await manager.on_disconnect()
