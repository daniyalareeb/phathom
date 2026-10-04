"""Phathom CLI — SPEC §7.2. Phase 1: test-audio. Phase 2: setup/process/calls/show."""

import asyncio
import logging
import os
import re
import shutil
import typer
from datetime import datetime, timezone
from pathlib import Path
from rich.console import Console
from rich.table import Table

from phathom.audio.devices import _run as run_cmd
from phathom.audio.devices import AudioDevices
from phathom.audio.player import Player
from phathom.audio.recorder import TrackRecorder
from phathom.config import settings
from phathom.logging_setup import setup_logging
from phathom.pipeline import mmss, speaker_label
from phathom.store import DatabaseBusy, Store, close_shared, make_call_id

app = typer.Typer()
console = Console()


def _run(coro) -> None:
    """asyncio.run + flush/close the embedded DB; friendly error if the server owns it."""
    async def main():
        try:
            await coro
        finally:
            await close_shared()
    try:
        asyncio.run(main())
    except DatabaseBusy as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.callback()
def _root() -> None:
    """Phathom — local WhatsApp call assistant."""


async def _mean_volume(wav: Path, start: float, dur: float) -> float | None:
    rc, _, err = await run_cmd(
        "ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(dur),
        "-i", str(wav), "-af", "volumedetect", "-f", "null", "-",
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", err)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return float("-inf")


async def _make_tone(path: Path, freq: int, duration: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await run_cmd(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-ar", "48000", "-ac", "1", str(path),
    )


async def _paplay(device: str, wav: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "paplay", f"--device={device}", str(wav),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


@app.command()
def test_audio() -> None:
    """Phase 1 self-test: loopback tones on both virtual devices, check levels."""
    asyncio.run(_test_audio())


async def _test_audio() -> None:
    setup_logging()
    log = logging.getLogger("phathom.test-audio")
    rows: list[tuple[str, str, str]] = []  # (check, result, detail)
    ok_all = True

    async def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        rows.append((name, "✅" if ok else "❌", detail))

    _, sink_before, _ = await run_cmd("pactl", "get-default-sink")
    _, source_before, _ = await run_cmd("pactl", "get-default-source")

    dev = AudioDevices(listen_live=settings.LISTEN_LIVE)
    await dev.setup()
    have_sinks = await dev._device_exists("sinks", "phathom_speaker") and await dev._device_exists("sinks", "phathom_voice")
    have_mic = await dev._device_exists("sources", "phathom_mic")
    await check("devices exist", have_sinks and have_mic, "phathom_speaker/phathom_voice/phathom_mic")

    outdir = Path("data/debug/test-audio")
    tone_voice = outdir / "tone_voice.wav"
    tone_speaker = outdir / "tone_speaker.wav"
    caller_wav = outdir / "caller.wav"
    bot_wav = outdir / "bot.wav"
    await _make_tone(tone_voice, 440)
    await _make_tone(tone_speaker, 880)

    rec_caller = TrackRecorder("phathom_speaker.monitor", caller_wav)
    rec_bot = TrackRecorder("phathom_voice.monitor", bot_wav)
    player = Player("phathom_voice")
    await rec_caller.start()
    await rec_bot.start()
    await asyncio.sleep(0.5)
    await player.play(tone_voice)      # T=0.5..3.5 on bot track
    await asyncio.sleep(1.0)           # gap
    await _paplay("phathom_speaker", tone_speaker)  # T=4.5..7.5 on caller track
    await asyncio.sleep(0.5)
    await rec_caller.stop()
    await rec_bot.stop()

    # Windows: voice tone present [1.0,3.0], absent [5.0,7.0]; speaker tone present [5.0,7.0], absent [1.0,3.0]
    bot_present = await _mean_volume(bot_wav, 1.0, 2.0)
    bot_absent = await _mean_volume(bot_wav, 5.0, 2.0)
    caller_present = await _mean_volume(caller_wav, 5.0, 2.0)
    caller_absent = await _mean_volume(caller_wav, 1.0, 2.0)

    def fmt(v: float | None) -> str:
        return f"{v:.1f} dB" if v is not None else "unreadable"

    await check("bot.wav has voice tone", bot_present is not None and bot_present > -40, fmt(bot_present))
    await check("bot.wav silent during speaker tone", bot_absent is not None and bot_absent < -60, fmt(bot_absent))
    await check("caller.wav has speaker tone", caller_present is not None and caller_present > -40, fmt(caller_present))
    await check("caller.wav silent during voice tone", caller_absent is not None and caller_absent < -60, fmt(caller_absent))

    await dev.teardown()
    _, sink_after, _ = await run_cmd("pactl", "get-default-sink")
    _, source_after, _ = await run_cmd("pactl", "get-default-source")
    await check("default sink unchanged", sink_before == sink_after, f"{sink_before} -> {sink_after}")
    await check("default source unchanged", source_before == source_after, f"{source_before} -> {source_after}")

    table = Table(title="phathom test-audio")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail")
    for name, res, detail in rows:
        table.add_row(name, res, detail)
    console.print(table)
    log.info("test-audio %s", "PASS" if ok_all else "FAIL")
    if not ok_all:
        raise typer.Exit(1)


@app.command()
def setup() -> None:
    """Check environment, .env, tools, Groq key and DB schema. Prints a ✅/❌ table."""
    _run(_setup())


async def _setup() -> None:
    setup_logging()
    rows: list[tuple[str, str, str]] = []
    ok_all = True

    async def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        rows.append((name, "✅" if ok else "❌", detail))

    for d in ["data/recordings", "data/tts-cache", "data/debug", "data/logs"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    await check("data dirs", True, "data/{recordings,tts-cache,debug,logs}")

    if not Path(".env").exists():
        shutil.copy(".env.example", ".env")
        await check(".env", True, "created from .env.example — add your GROQ_API_KEY")
    else:
        await check(".env", True, "exists")

    for tool in ["parec", "paplay", "ffmpeg"]:
        await check(tool, shutil.which(tool) is not None,
                   shutil.which(tool) or f"install: apt install { 'pulseaudio-utils' if tool != 'ffmpeg' else 'ffmpeg'}")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    await check("chrome", chrome is not None, chrome or "install Google Chrome (or set CHROME_CHANNEL=chromium)")

    key_ok, key_detail = False, "GROQ_API_KEY missing in .env"
    if settings.GROQ_API_KEY:
        try:
            from groq import AsyncGroq
            models = await AsyncGroq(api_key=settings.GROQ_API_KEY, max_retries=0).models.list()
            key_ok = True
            key_detail = f"key works ({len(models.data)} models)"
        except Exception as e:
            key_detail = f"key test failed: {e!r}"
    await check("groq key", key_ok, key_detail)

    db_ok, db_detail = False, ""
    try:
        store = await Store().connect()
        db_ok = True
        db_detail = f"schema bootstrapped on {settings.SURREAL_URL}"
        await store.close()
    except Exception as e:
        db_detail = (str(e) if isinstance(e, DatabaseBusy)
                     else f"DB failed to open ({settings.SURREAL_URL}): {e!r}")
    await check("surrealdb", db_ok, db_detail)

    table = Table(title="phathom setup")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail")
    for name, res, detail in rows:
        table.add_row(name, res, detail)
    console.print(table)
    if not ok_all:
        raise typer.Exit(1)


@app.command()
def process(wav_path: str, caller: str = typer.Option("Unknown", "--caller"),
            from_json: str = typer.Option(None, "--from-json",
                                          help="import a result.json written while the DB was down")) -> None:
    """Run the after-call pipeline on any wav file (no real call needed)."""
    _run(_process(wav_path, caller, from_json))


async def _process(wav_arg: str, caller_label: str, from_json: str | None) -> None:
    from phathom.pipeline import import_result_json, run_pipeline
    setup_logging()
    store = await Store().connect()
    try:
        if from_json:
            call_id = await import_result_json(Path(from_json), store)
            console.print(f"imported {call_id}")
            return
        wav_path = Path(wav_arg)
        if not wav_path.exists():
            console.print(f"[red]no such file: {wav_path}[/red]")
            raise typer.Exit(1)
        call_id = make_call_id(caller_label)
        audio_dir = Path("data/recordings") / call_id
        audio_dir.mkdir(parents=True, exist_ok=True)
        dest = audio_dir / "caller.wav"
        # normalise to 16 kHz mono wav for STT
        rc, _, err = await run_cmd(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(wav_path), "-ac", "1", "-ar", "16000", str(dest))
        if rc != 0:
            console.print(f"[red]ffmpeg failed: {err}[/red]")
            raise typer.Exit(1)
        import wave
        from datetime import timedelta
        with wave.open(str(dest)) as w:
            length_s = w.getnframes() / w.getframerate()
        started = datetime.now(timezone.utc)
        await store.create_call(call_id, caller_label, started.isoformat(), "manual",
                                status="live", audio_dir=str(audio_dir))
        console.print(f"processing call {call_id} ...")
        await run_pipeline(call_id, caller_label=caller_label, mode="manual",
                           started_at=started.isoformat(), audio_dir=audio_dir,
                           caller_wav=dest, store=store,
                           ended_at=(started + timedelta(seconds=length_s)).isoformat())
        call = await store.get_call(call_id)
        console.print(f"status: [bold]{call.get('status')}[/bold]  title: {call.get('title')}")
        if call.get("status") == "failed":
            console.print(f"[red]{call.get('error')}[/red]")
    finally:
        await store.close()


@app.command()
def calls(limit: int = typer.Option(20, "--limit")) -> None:
    """Table of recent calls: id, date, caller, duration, mode, title."""
    _run(_calls(limit))


async def _calls(limit: int) -> None:
    store = await Store().connect()
    try:
        rows = await store.list_calls(limit)
        table = Table(title=f"phathom calls (last {limit})")
        for col in ["id", "date", "caller", "dur", "mode", "title"]:
            table.add_column(col)
        for r in rows:
            cid = str(r.get("id", "")).removeprefix("call:")
            started = str(r.get("started_at", ""))[:16].replace("T", " ")
            dur = r.get("duration_s")
            table.add_row(cid, started, str(r.get("caller", "")),
                          f"{dur}s" if dur is not None else "—",
                          str(r.get("mode", "")), str(r.get("title") or ""))
        console.print(table)
    finally:
        await store.close()


@app.command()
def show(call_id: str) -> None:
    """Summary, action items, message for owner, full transcript with mm:ss times."""
    _run(_show(call_id))


async def _show(call_id: str) -> None:
    store = await Store().connect()
    try:
        call = await store.get_call(call_id)
        if not call:
            # allow short id without date? try substring match
            rows = await store.query(
                "SELECT * FROM call WHERE string::contains(id, $frag) LIMIT 2", {"frag": call_id})
            call = rows[0] if len(rows) == 1 else None
            if not call:
                console.print(f"[red]no such call: {call_id}[/red]")
                raise typer.Exit(1)
        real_id = str(call["id"]).removeprefix("call:")
        console.print(f"[bold]{call.get('title') or '(untitled)'}[/bold]  {call.get('caller')}  "
                      f"{str(call.get('started_at', ''))[:16]}  mode={call.get('mode')} status={call.get('status')}")
        console.print(f"\n{call.get('summary') or ''}")
        if call.get("key_points"):
            console.print("\n[bold]Key points:[/bold]")
            for kp in call["key_points"]:
                console.print(f"  • {kp}")
        if call.get("action_items"):
            console.print("\n[bold]Action items:[/bold]")
            for a in call["action_items"]:
                due = f" (due {a.get('due')})" if a.get("due") else ""
                console.print(f"  • [{a.get('owner')}] {a.get('item')}{due}")
        if call.get("message_for_owner"):
            console.print(f"\n[bold]Message for owner:[/bold] {call.get('message_for_owner')}")
        segs = await store.get_segments(real_id)
        console.print("\n[bold]Transcript:[/bold]")
        for s in segs:
            who = speaker_label(s["speaker"])
            console.print(f"  [{mmss(s['t_start'])}] {who}: {s['text']}")
    finally:
        await store.close()


@app.command()
def login() -> None:
    """Open WhatsApp Web in the persistent profile, headed; wait for login, then close."""
    asyncio.run(_login())


async def _login() -> None:
    from phathom.whatsapp.driver import WhatsAppDriver
    setup_logging()
    driver = WhatsAppDriver()
    try:
        await driver.start(headed=True)
        console.print("Scan the QR code if shown, then wait...")
        await driver.wait_for_login(timeout_s=300)
        await driver.debug_dump("logged_in")
        console.print("[green]Logged in. Profile saved.[/green]")
    finally:
        await driver.close()


@app.command()
def start(background: bool = typer.Option(False, "--background"),
          legacy: bool = typer.Option(False, "--legacy",
                                      help="legacy Playwright/PulseAudio path (v1, kept working)")) -> None:
    """Run the daemon in the foreground (Ctrl+C stops it).

    The daemon IS the legacy v1 path (separate Chrome via Playwright +
    PulseAudio routing); --legacy just makes that explicit. For the v2
    extension path, run `phathom serve` instead.
    """
    from phathom import state as state_mod
    st = state_mod.read()
    if not os.environ.get("PHATHOM_DAEMON_CHILD") and state_mod.pid_alive(st.get("daemon_pid")):
        console.print(f"[red]daemon already running (pid {st['daemon_pid']})[/red]")
        raise typer.Exit(1)
    if background:
        import subprocess
        import sys
        exe = shutil.which("phathom") or sys.argv[0]
        logf = open("data/logs/daemon.out", "ab")
        child = subprocess.Popen(
            [exe, "start"], stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "PHATHOM_DAEMON_CHILD": "1"})
        state_mod.write({"daemon_pid": child.pid, "started_at": state_mod.now_iso()})
        console.print(f"daemon started in background (pid {child.pid})")
        return
    _run(_start_foreground())


async def _start_foreground() -> None:
    from phathom.daemon import run_daemon
    await run_daemon()


@app.command()
def stop() -> None:
    """SIGTERM the daemon; wait up to 10s; clear the pid."""
    import signal
    import time
    from phathom import state as state_mod
    st = state_mod.read()
    pid = st.get("daemon_pid")
    if not state_mod.pid_alive(pid):
        console.print("daemon not running")
        state_mod.write({"daemon_pid": None, "current_call": None})
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if not state_mod.pid_alive(pid):
            break
    else:
        console.print("[red]daemon did not exit in 10s[/red]")
    state_mod.write({"daemon_pid": None, "current_call": None})
    console.print("daemon stopped")


@app.command()
def status() -> None:
    """Mode, daemon alive?, current call, WhatsApp logged in?, last 3 calls."""
    _run(_status())


async def _status() -> None:
    from phathom import state as state_mod
    st = state_mod.read()
    alive = state_mod.pid_alive(st.get("daemon_pid"))
    console.print(f"mode: [bold]{st.get('mode')}[/bold]")
    console.print(f"copilot (notes on your own calls): {'on' if st.get('copilot', True) else 'off'}")
    console.print(f"daemon: {'alive (pid %s)' % st['daemon_pid'] if alive else 'not running'}")
    cur = st.get("current_call")
    console.print(f"current call: {cur['caller']} since {cur['since'][:19]}" if cur else "current call: none")
    console.print(f"whatsapp logged in: {st.get('wa_logged_in')}")
    try:
        store = await Store().connect()
        try:
            for r in await store.list_calls(3):
                console.print(f"  {str(r.get('id','')).removeprefix('call:')}  {r.get('caller')}  "
                              f"{r.get('status')}  {r.get('title') or ''}")
        finally:
            await store.close()
    except Exception as e:  # noqa: BLE001
        console.print(f"(call history unavailable: {e!r})")


@app.command()
def mode(value: str) -> None:
    """Update mode in state.json (takes effect on the next call, no restart)."""
    from phathom import state as state_mod
    if value not in ("off", "notes", "assistant"):
        console.print("[red]mode must be off|notes|assistant[/red]")
        raise typer.Exit(1)
    state_mod.write({"mode": value})
    console.print(f"mode: {value}")


@app.command()
def copilot(value: str) -> None:
    """on|off: record + summarise calls you take or make yourself in the WhatsApp window."""
    from phathom import state as state_mod
    if value not in ("on", "off"):
        console.print("[red]copilot must be on|off[/red]")
        raise typer.Exit(1)
    state_mod.write({"copilot": value == "on"})
    console.print(f"copilot: {value}")


@app.command()
def ask(question: str) -> None:
    """Q&A over all calls (full-text search + smart summary)."""
    _run(_ask(question))


async def _ask(question: str) -> None:
    from phathom.ai.llm import answer_question
    setup_logging()
    store = await Store().connect()
    try:
        segs = await store.query(
            "SELECT id, call, t_start, speaker, text, search::score(1) AS score"
            " FROM segment WHERE text @1@ $q ORDER BY score DESC LIMIT 25", {"q": question})
        calls = await store.query(
            "SELECT id, caller, started_at, title, summary, message_for_owner,"
            " search::score(1) AS score FROM call WHERE summary @1@ $q"
            " ORDER BY score DESC LIMIT 8", {"q": question})
        evidence: list[dict] = []
        seen_calls: set[str] = set()

        def cid_of(row) -> str:
            rid = row.get("call", row.get("id"))
            return str(rid).removeprefix("call:")

        for r in segs or []:
            cid = cid_of(r)
            seen_calls.add(cid)
            evidence.append({"kind": "segment", "call_id": cid, "caller": "?",
                             "date": "", "t_start": r.get("t_start", 0),
                             "speaker": r.get("speaker", ""), "text": r.get("text", "")})
        callers = {str(r.get("id", "")).removeprefix("call:"): r for r in calls or []}
        # attach caller/date to segment evidence
        if seen_calls:
            for cid in list(seen_calls):
                c = await store.get_call(cid)
                if c:
                    for e in evidence:
                        if e["call_id"] == cid:
                            e["caller"] = c.get("caller", "?")
                            e["date"] = str(c.get("started_at", ""))[:10]
        for cid, r in callers.items():
            seen_calls.add(cid)
            evidence.append({"kind": "summary", "call_id": cid, "caller": r.get("caller", "?"),
                             "date": str(r.get("started_at", ""))[:10],
                             "title": r.get("title") or "", "summary": r.get("summary") or ""})
        # name match: question names a caller -> their 5 most recent calls
        ql = question.lower()
        try:
            all_callers = await store.query("SELECT caller FROM call GROUP BY caller")
            names = {str(r.get("caller", "")) for r in all_callers or [] if r.get("caller")}
            for name in names:
                if name.lower() in ql:
                    recent = await store.query(
                        "SELECT id, caller, started_at, title, summary FROM call"
                        " WHERE caller = $name ORDER BY started_at DESC LIMIT 5", {"name": name})
                    for r in recent or []:
                        cid = str(r.get("id", "")).removeprefix("call:")
                        if cid not in seen_calls:
                            seen_calls.add(cid)
                            evidence.append({"kind": "summary", "call_id": cid, "caller": name,
                                             "date": str(r.get("started_at", ""))[:10],
                                             "title": r.get("title") or "",
                                             "summary": r.get("summary") or ""})
                    break
        except Exception as e:  # noqa: BLE001
            console.print(f"(name match skipped: {e!r})")
        if not evidence:
            recent = await store.query(
                "SELECT id, caller, started_at, title, summary FROM call"
                " ORDER BY started_at DESC LIMIT 10")
            for r in recent or []:
                evidence.append({"kind": "summary",
                                 "call_id": str(r.get("id", "")).removeprefix("call:"),
                                 "caller": r.get("caller", "?"),
                                 "date": str(r.get("started_at", ""))[:10],
                                 "title": r.get("title") or "", "summary": r.get("summary") or ""})
        try:  # §8.4: Ask also covers chat messages + brains
            chat_hits = await store.query(
                "SELECT chat, sender, ts, text, search::score(1) AS score"
                " FROM message WHERE text @1@ $q ORDER BY score DESC LIMIT 15", {"q": question})
            for r in chat_hits or []:
                cid = str(r.get("chat", "")).removeprefix("chat:")
                evidence.append({"kind": "chat", "chat": cid, "sender": r.get("sender", ""),
                                 "date": str(r.get("ts", ""))[:10], "text": r.get("text", "")})
        except Exception as e:  # noqa: BLE001
            console.print(f"(chat evidence skipped: {e!r})")
        answer = await answer_question(question, evidence)
        console.print(f"\n{answer}\n")
        cited = sorted({e.get("call_id", e.get("chat", "?")) for e in evidence})
        console.print(f"[dim]sources: {', '.join(cited) if cited else 'none'}[/dim]")
    finally:
        await store.close()


@app.command()
def brief(text: str = typer.Argument(None),
          append: bool = typer.Option(False, "--append", help="append a line instead of overwriting")) -> None:
    """No arg: print brief.md. With arg: overwrite it (--append adds a line)."""
    from pathlib import Path as _Path
    p = _Path("brief.md")
    if text is None:
        console.print(p.read_text() if p.exists() else "(no brief.md yet)")
        return
    if append and p.exists():
        p.write_text(p.read_text().rstrip("\n") + "\n" + text + "\n")
    else:
        p.write_text(text.rstrip("\n") + "\n")
    console.print("[green]brief updated[/green]")


@app.command()
def serve(port: int = typer.Option(8765, "--port"),
          spike: bool = typer.Option(False, "--spike", help="run the Phase-0 spike server instead")) -> None:
    """v2 local server on 127.0.0.1 only (token auth). Use --spike for the Phase-0 capture server."""
    from phathom.logging_setup import setup_logging
    if spike:
        from phathom.serve_spike import run as run_spike
        setup_logging()
        run_spike(port=port)
        return
    from phathom.server.app import run as run_server
    run_server(port=port)


@app.command()
def token() -> None:
    """Print the extension token (paste once into the extension's first-run screen)."""
    from phathom.server.auth import get_or_create_token
    console.print(get_or_create_token())
