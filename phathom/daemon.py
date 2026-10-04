"""Daemon: main loop + call state machine — SPEC §2.3 / §7.3 / §12.

Bot calls: notes mode (answer -> disclosure -> record) or assistant mode (live agent).
Owner calls (copilot): the owner answers or places a call himself in the WhatsApp window;
his mic/speakers are patched through and, if copilot is on, both sides are recorded.
"""

import asyncio
import logging
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from phathom import contacts, state
from phathom.ai.prompts import fixed_line
from phathom.ai.tts import presynth_fixed_lines, synth
from phathom.audio.devices import AudioDevices
from phathom.audio.player import Player
from phathom.audio.recorder import TrackRecorder
from phathom.config import settings
from phathom.pipeline import notify, run_pipeline
from phathom.store import Store, make_call_id
from phathom.whatsapp.driver import WhatsAppDriver

log = logging.getLogger("phathom.daemon")

OWNER_CALL_MAX_S = 4 * 3600  # safety cap on recording the owner's own calls


class Daemon:
    def __init__(self):
        self.driver = WhatsAppDriver()
        self.devices = AudioDevices(listen_live=settings.LISTEN_LIVE)
        self.player = Player("phathom_voice")
        self.store: Store | None = None
        self._stop = asyncio.Event()
        self._in_call = False
        self._shutdown_hangup = False
        self._pipeline_tasks: set[asyncio.Task] = set()
        self._crash_at: float = 0.0
        self._crashes = 0
        self._ignored_ring: str | None = None  # log an ignored ring once, not every poll

    # ---- lifecycle ----

    async def run(self) -> None:
        from phathom.logging_setup import setup_logging
        setup_logging()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass
        st = state.read()
        import os
        if not os.environ.get("PHATHOM_DAEMON_CHILD") and state.pid_alive(st.get("daemon_pid")):
            raise RuntimeError(f"daemon already running (pid {st['daemon_pid']}) — run 'phathom stop' first")
        state.write({"daemon_pid": os.getpid(), "started_at": state.now_iso(), "current_call": None})
        try:
            await self.devices.setup()
            await presynth_fixed_lines()
            await self._connect_store()
            await self.driver.start(headed=True)
            logged_in = await self._wait_logged_in_briefly()
            state.write({"wa_logged_in": logged_in})
            if not logged_in:
                await notify("Phathom: WhatsApp logged out — run 'phathom login'")
                log.warning("WhatsApp not logged in at start; staying idle")
            await self._loop()
        finally:
            await self.shutdown()

    async def _connect_store(self) -> None:
        """Connect with retries (3x over ~30s); None if DB is down (SPEC §12)."""
        for attempt in range(3):
            try:
                self.store = await Store().connect()
                log.info("database connected")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("DB connect attempt %d failed: %r", attempt + 1, e)
                await asyncio.sleep(10)
        log.error("database unreachable after 3 attempts; will answer+record, pipeline goes offline")
        self.store = None

    async def _wait_logged_in_briefly(self, timeout_s: int = 30) -> bool:
        for _ in range(timeout_s * 2):
            if await self.driver.is_logged_in():
                return True
            await asyncio.sleep(0.5)
        return await self.driver.is_logged_in()

    async def shutdown(self) -> None:
        log.info("daemon shutting down")
        self._stop.set()
        if self._in_call and not self._shutdown_hangup:
            self._shutdown_hangup = True
            try:
                await self.driver.hang_up()
            except Exception as e:  # noqa: BLE001
                log.warning("shutdown hangup: %r", e)
        if self._pipeline_tasks:
            log.info("waiting for %d pipeline(s) (max 120s)", len(self._pipeline_tasks))
            try:
                await asyncio.wait_for(asyncio.gather(*self._pipeline_tasks,
                                                      return_exceptions=True), timeout=120)
            except asyncio.TimeoutError:
                log.warning("pipeline did not finish in 120s; leaving it")
        try:
            await self.devices.teardown()
        except Exception as e:  # noqa: BLE001
            log.warning("devices teardown: %r", e)
        try:
            await self.driver.close()
        except Exception as e:  # noqa: BLE001
            log.warning("browser close: %r", e)
        if self.store is not None:
            try:
                await self.store.close()
            except Exception:  # noqa: BLE001
                pass
            self.store = None
        st = state.read()
        import os
        if st.get("daemon_pid") == os.getpid():
            state.write({"daemon_pid": None, "current_call": None})
        log.info("daemon stopped")

    # ---- main loop ----

    async def _loop(self) -> None:
        last_login_check = 0.0
        log.info("daemon idle (mode=%s)", state.read().get("mode"))
        while not self._stop.is_set():
            try:
                await self._tick(last_login_check)
                now = time.monotonic()
                if now - last_login_check > 60:
                    last_login_check = now
                    await self._login_watch()
            except Exception as e:  # noqa: BLE001
                log.exception("loop error: %r", e)
                await self._handle_driver_crash()
            await asyncio.sleep(0.5)

    async def _login_watch(self) -> None:
        try:
            ok = await self.driver.is_logged_in()
        except Exception as e:  # noqa: BLE001
            log.warning("login check failed: %r", e)
            return
        state.write({"wa_logged_in": ok})
        if not ok:
            await notify("Phathom: WhatsApp logged out — run 'phathom login'")
            log.warning("WhatsApp logged out; staying idle")

    async def _handle_driver_crash(self) -> None:
        now = time.monotonic()
        if now - self._crash_at > 300:
            self._crashes = 0
        self._crash_at = now
        self._crashes += 1
        try:
            await self.driver.debug_dump("crash")
        except Exception:  # noqa: BLE001
            pass
        if self._crashes > 2:
            await notify("Phathom: browser keeps crashing — exiting")
            log.error("browser crashed repeatedly; exiting non-zero (systemd restarts)")
            raise SystemExit(1)
        log.warning("restarting browser (attempt %d)", self._crashes)
        try:
            await self.driver.close()
        except Exception:  # noqa: BLE001
            pass
        await self.driver.start(headed=True)

    async def _tick(self, _last_login_check: float) -> None:
        inc = await self.driver.poll_incoming_call()
        if inc is None:
            self._ignored_ring = None
            # A connected call we didn't answer = the owner answered or dialled in this window.
            if await self.driver.is_call_active():
                await self._owner_call()
            return
        mode = state.read().get("mode", settings.DEFAULT_MODE)
        if mode == "off" or not contacts.should_answer(inc.caller, mode):
            if self._ignored_ring != inc.caller:
                self._ignored_ring = inc.caller
                log.info("not answering call from %r (mode=%s)", inc.caller, mode)
            return
        await self._handle_ringing(inc.caller, inc.is_video, mode)

    async def _ring_present(self) -> bool:
        return await self.driver.poll_incoming_call() is not None

    async def _handle_ringing(self, caller: str, is_video: bool, mode: str) -> None:
        log.info("RINGING from %r (video=%s, mode=%s)", caller, is_video, mode)
        delay = settings.ANSWER_DELAY_S
        for _ in range(int(delay * 2)):
            await asyncio.sleep(0.5)
            if self._stop.is_set():
                return
            # mode re-read every call (§7.1); honour changes mid-ring
            mode = state.read().get("mode", mode)
            if mode == "off" or not contacts.should_answer(caller, mode):
                log.info("mode changed mid-ring; ignoring call from %r", caller)
                return
            if not await self._ring_present():
                if await self.driver.is_call_active():
                    log.info("owner answered %r in the WhatsApp window", caller)
                    await self._owner_call(caller)
                else:
                    log.info("ring UI gone before answer (answered on phone / hung up)")
                return
        await self._answer(caller, is_video, mode)

    async def _answer(self, caller: str, is_video: bool, mode: str) -> None:
        if shutil.disk_usage("data").free < 1024 ** 3:
            await notify("Phathom: disk < 1 GB free — not recording; letting call ring")
            log.warning("disk low; ignoring call from %r", caller)
            return
        try:
            await self.driver.accept(audio_only=True)
        except Exception as e:  # noqa: BLE001
            log.warning("accept failed: %r", e)
            await self.driver.debug_dump("accept_error")
            return
        if is_video:
            log.info("video call accepted audio-only (camera stays off)")
        await self._record_call(caller, mode if mode in ("notes", "assistant") else "notes")

    async def _owner_call(self, caller: str | None = None) -> None:
        """Owner is talking himself. Always patch his mic/speakers through (Chrome is wired to
        the virtual devices, so without this nobody hears anyone); record only if copilot is on."""
        caller = caller or await self.driver.current_peer_name() or "Unknown"
        copilot = bool(state.read().get("copilot", True))
        log.info("OWNER CALL with %r (copilot=%s)", caller, copilot)
        await self.devices.enable_passthrough()
        try:
            if copilot:
                await self._record_call(caller, "copilot")
            else:
                state.write({"current_call": {"id": None, "caller": caller,
                                              "since": state.now_iso(), "mode": "owner"}})
                await self._wait_call_end(OWNER_CALL_MAX_S)
                state.write({"current_call": None})
        finally:
            await self.devices.disable_passthrough()

    async def _wait_call_end(self, max_s: float) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set() and time.monotonic() - t0 < max_s:
            if not await self.driver.is_call_active():
                log.info("call ended")
                return
            await asyncio.sleep(0.5)

    # ---- recorded call (notes / assistant / copilot) ----

    def _greet_lang(self, caller: str) -> str:
        notes = contacts.notes_for(caller) or ""
        if "urdu" in notes.lower() or "urdu" in caller.lower():
            return "ur"
        return "en"

    def _disclosure_lang(self, caller: str) -> str:
        return self._greet_lang(caller)

    def _read_brief(self) -> str:
        try:
            return Path("brief.md").read_text().strip()
        except OSError:
            return ""

    async def _record_call(self, caller: str, mode: str) -> None:
        owner_on_call = mode == "copilot"
        self._in_call = not owner_on_call  # shutdown hangs up bot calls only, never the owner's
        started = datetime.now(timezone.utc)
        call_id = make_call_id(caller)
        audio_dir = Path("data/recordings") / call_id
        audio_dir.mkdir(parents=True, exist_ok=True)
        caller_wav = audio_dir / "caller.wav"
        # Our side of the call: the bot's voice, or (copilot) the owner's mic patched into phathom_voice.
        own_wav = audio_dir / ("owner.wav" if owner_on_call else "bot.wav")
        state.write({"current_call": {"id": call_id, "caller": caller,
                                      "since": started.isoformat(), "mode": mode}})
        if self.store is not None:
            try:
                await self.store.create_call(call_id, caller, started.isoformat(), mode,
                                             status="live", audio_dir=str(audio_dir))
            except Exception as e:  # noqa: BLE001
                log.warning("create_call failed (continuing offline for this call): %r", e)
        else:
            log.warning("no DB; recording call %s offline", call_id)
        queue: asyncio.Queue[bytes] | None = asyncio.Queue(maxsize=200) if mode == "assistant" else None
        rec_caller = TrackRecorder("phathom_speaker.monitor", caller_wav, queue)
        rec_own = TrackRecorder("phathom_voice.monitor", own_wav)
        live_bot_segments: list[dict] = []
        disclosure_played = False
        try:
            moved = await self.devices.route_chrome()
            log.info("route_chrome moved=%s", moved)
            await rec_caller.start()
            await rec_own.start()
            if owner_on_call:
                await self._wait_call_end(OWNER_CALL_MAX_S)
            else:
                await asyncio.sleep(1.0)  # settle after connect
                if mode == "assistant":
                    result = await self._assistant_call(
                        call_id, caller, queue, audio_dir, caller_wav, own_wav)
                    live_bot_segments = result["bot_segments"]
                    disclosure_played = result["disclosure_played"]
                else:
                    disclosure_played = await self._notes_record(caller)
        finally:
            ended_at = datetime.now(timezone.utc).isoformat()
            if not owner_on_call:  # never hang up on the owner's own call
                try:
                    if await self.driver.is_call_active():
                        await self.driver.hang_up()
                except Exception as e:  # noqa: BLE001
                    log.warning("hangup: %r", e)
            await rec_caller.stop()
            await rec_own.stop()
            self._in_call = False
            state.write({"current_call": None})
        task = asyncio.create_task(self._post_call(
            call_id, caller, mode, started.isoformat(), audio_dir, caller_wav,
            None if owner_on_call else own_wav, disclosure_played, live_bot_segments,
            owner_wav=own_wav if owner_on_call else None, ended_at=ended_at))
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)
        log.info("call %s handed to pipeline; daemon idle", call_id)

    async def _assistant_call(self, call_id: str, caller: str, queue: asyncio.Queue,
                              audio_dir: Path, caller_wav: Path, bot_wav: Path) -> dict:
        """Run the live agent; supervise hangup/stop. Returns bot segments + flags."""
        from phathom.agent import LiveAgent
        agent = LiveAgent(
            call_id=call_id, caller=caller, contact_notes=contacts.notes_for(caller),
            brief=self._read_brief(), frame_queue=queue, player=self.player,
            store=self.store, call_active=self.driver.is_call_active,
            greet_lang=self._greet_lang(caller))
        agent_task = asyncio.create_task(agent.run())
        try:
            while not agent_task.done():
                if self._stop.is_set():
                    agent.cancel()
                    break
                await asyncio.sleep(0.5)
            result = await agent_task
        except asyncio.CancelledError:
            agent.cancel()
            result = await agent_task
        log.info("agent ended: reason=%s latencies_ms=%s",
                 result.end_reason, [int(x) for x in agent._latencies])
        if agent._latencies:
            med = sorted(agent._latencies)[len(agent._latencies) // 2]
            log.info("median turn latency: %dms", med)
        return {"bot_segments": agent.bot_segments, "disclosure_played": True}

    async def _notes_record(self, caller: str) -> bool:
        """Notes mode: disclosure once, then record until hangup or MAX_CALL_S."""
        disclosure_played = False
        if settings.DISCLOSURE:
            try:
                lang = self._disclosure_lang(caller)
                wav = await synth(fixed_line("disclosure", lang), lang)
                await self.player.play(wav)
                disclosure_played = True
            except Exception as e:  # noqa: BLE001
                log.warning("disclosure playback failed: %r", e)
        t0 = time.monotonic()
        while not self._stop.is_set():
            if not await self.driver.is_call_active():
                log.info("call ended by peer")
                break
            if time.monotonic() - t0 > settings.MAX_CALL_S:
                log.info("MAX_CALL_S reached; playing goodbye and hanging up")
                try:
                    lang = self._disclosure_lang(caller)
                    wav = await synth(fixed_line("goodbye", lang), lang)
                    await self.player.play(wav)
                except Exception as e:  # noqa: BLE001
                    log.warning("goodbye playback failed: %r", e)
                break
            await asyncio.sleep(0.5)
        return disclosure_played

    async def _post_call(self, call_id: str, caller: str, mode: str, started_at: str,
                         audio_dir: Path, caller_wav: Path, bot_wav: Path | None,
                         disclosure_played: bool, live_bot_segments: list[dict] | None = None,
                         owner_wav: Path | None = None, ended_at: str | None = None) -> None:
        from phathom.pipeline import run_offline
        if self.store is not None:
            try:
                await run_pipeline(call_id, caller_label=caller, mode=mode, started_at=started_at,
                                   audio_dir=audio_dir, caller_wav=caller_wav, bot_wav=bot_wav,
                                   live_bot_segments=live_bot_segments,
                                   disclosure_played=disclosure_played, store=self.store,
                                   owner_wav=owner_wav, ended_at=ended_at)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("online pipeline failed, trying offline: %r", e)
        try:
            result = await run_offline(call_id, caller_label=caller, mode=mode, started_at=started_at,
                                       audio_dir=audio_dir, caller_wav=caller_wav, bot_wav=bot_wav,
                                       live_bot_segments=live_bot_segments,
                                       disclosure_played=disclosure_played,
                                       owner_wav=owner_wav, ended_at=ended_at)
            (audio_dir / "result.json").write_text(
                __import__("json").dumps(result, indent=2, default=str))
            await notify("Phathom · saved offline",
                         f"{caller}: DB unreachable, result.json kept. Import later with phathom process --from-json.")
        except Exception as e:  # noqa: BLE001
            log.exception("offline pipeline failed for %s", call_id)
            await notify("Phathom · processing failed", f"{caller}: {e!r}. Audio kept.")


async def run_daemon() -> None:
    await Daemon().run()
