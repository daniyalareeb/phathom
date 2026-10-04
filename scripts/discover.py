"""Phase 0 discovery helper (throwaway, per SPEC §11 Phase 0).

Usage:
  .venv/bin/python scripts/discover.py --mode login   # open WA Web, wait for QR scan
  .venv/bin/python scripts/discover.py --mode watch   # 30s dump loop for a test call

Dumps screenshots + HTML from EVERY open page/context to data/debug/.
Never clicks anything — selector discovery happens by inspecting the dumps.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from phathom.audio.devices import AudioDevices  # noqa: E402
from phathom.audio.player import Player  # noqa: E402
from phathom.audio.recorder import TrackRecorder  # noqa: E402
from phathom.logging_setup import setup_logging  # noqa: E402
from phathom.whatsapp.driver import launch_browser  # noqa: E402

log = setup_logging()
logging.getLogger("phathom").setLevel(logging.INFO)


async def dump_all(ctx, tag: str) -> Path:
    outdir = Path("data/debug")
    outdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for i, page in enumerate(ctx.pages):
        try:
            png = outdir / f"{ts}_{tag}_p{i}.png"
            html = outdir / f"{ts}_{tag}_p{i}.html"
            await page.screenshot(path=str(png))
            html.write_text(await page.content(), encoding="utf-8")
            log.info("dump %s url=%s title=%s -> %s + %s", tag, page.url, await page.title(), png, html)
        except Exception as e:  # noqa: BLE001
            log.warning("dump failed page %d: %r", i, e)
    return outdir


async def mode_login() -> None:
    async with async_playwright() as pw:
        ctx = await launch_browser(pw)
        seen_pages: set[str] = set()

        def on_page(page):
            log.info("NEW PAGE opened: url=%s", page.url)
            seen_pages.add(page.url)

        ctx.on("page", on_page)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://web.whatsapp.com")
        log.info("Opened WhatsApp Web. If you see a QR code, scan it with your phone.")
        log.info("Waiting up to 300s for login. Press Ctrl+C once logged in and chats are visible.")
        try:
            for tick in range(60):
                await asyncio.sleep(5)
                log.info("... still waiting (%ds). pages=%d urls=%s", (tick + 1) * 5, len(ctx.pages), [p.url for p in ctx.pages])
                for p in ctx.pages:
                    if p.url not in seen_pages:
                        log.info("NEW PAGE detected: url=%s title=%s", p.url, await p.title())
                        seen_pages.add(p.url)
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        await dump_all(ctx, "logged_in")
        log.info("Dumped logged_in. Close the browser window or press Ctrl+C to exit.")
        try:
            await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await ctx.close()


async def mode_accept(record_secs: int = 0, play_wav: str | None = None) -> None:
    """Detect ring, print caller, click Accept, dump in-call UI, hang up.
    With record_secs: virtual audio is set up BEFORE launch; both tracks are
    recorded for record_secs, play_wav is played into the call (Phase 1 test)."""
    import re
    import time

    async with async_playwright() as pw:
        dev: AudioDevices | None = None
        if record_secs:
            dev = AudioDevices()
            await dev.setup()
            log.info("audio devices ready before browser launch")
        ctx = await launch_browser(pw)

        def on_page(page):
            log.info("NEW PAGE/popup detected! url=%s", page.url)

        ctx.on("page", on_page)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if "whatsapp" not in page.url:
            await page.goto("https://web.whatsapp.com")
        log.info("Polling up to 120s for an incoming call. CALL NOW from your second phone.")
        accept_btn = page.get_by_role("button", name=re.compile(r"^Accept$", re.I))
        found = False
        for tick in range(240):
            await asyncio.sleep(0.5)
            try:
                if await accept_btn.count() > 0 and await accept_btn.first.is_visible():
                    found = True
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("poll tick %d: %r", tick, e)
            if tick % 20 == 0:
                log.info("... polling (%ds), pages=%d", tick // 2, len(ctx.pages))
        if not found:
            log.info("NO CALL detected in 120s. Closing.")
            await ctx.close()
            if dev is not None:
                await dev.teardown()
            return
        # Caller label from the live page (never hard-coded)
        try:
            caller_el = page.get_by_test_id("voip-call-participant-info-name")
            caller = (await caller_el.first.inner_text()).strip()
        except Exception as e:  # noqa: BLE001
            caller = f"<unreadable: {e!r}>"
        log.info("INCOMING CALL from caller=%r — dumping ring UI, then clicking Accept.", caller)
        print(f"CALLER: {caller}")
        await dump_all(ctx, "accept_ring")
        await accept_btn.first.click()
        log.info("Clicked Accept.")
        recs: list[TrackRecorder] = []
        if dev is not None:
            await dev.route_chrome()
            outdir = Path("data/debug/phase1_call")
            recs = [
                TrackRecorder("phathom_speaker.monitor", outdir / "caller.wav"),
                TrackRecorder("phathom_voice.monitor", outdir / "bot.wav"),
            ]
            for r in recs:
                await r.start()
            t0 = time.monotonic()
            if play_wav:
                finished = await Player("phathom_voice").play(Path(play_wav))
                log.info("TTS playback finished=%s", finished)
            remain = record_secs - (time.monotonic() - t0)
            if remain > 0:
                await asyncio.sleep(remain)
            for r in recs:
                await r.stop()
            log.info("recorded %ds: %s", record_secs, outdir)
        else:
            log.info("Waiting 10s in call, then dumping in-call UI.")
            await asyncio.sleep(10)
        await dump_all(ctx, "in_call")
        # Enumerate in-call buttons (discovery for HANGUP + camera toggle)
        btns = page.get_by_role("button")
        try:
            n = await btns.count()
        except Exception:  # noqa: BLE001
            n = 0
        log.info("IN-CALL BUTTON COUNT: %d", n)
        for i in range(min(n, 40)):
            try:
                name = await btns.nth(i).get_attribute("aria-label")
                log.info("in-call btn %d: aria-label=%r", i, name)
            except Exception as e:  # noqa: BLE001
                log.info("in-call btn %d: ERROR %r", i, e)
        # Hang up: click first button matching hangup-like names, else leave for human
        hung = False
        for pat in [r"End call", r"Hang up", r"^End$", r"Leave", r"Decline"]:
            cand = page.get_by_role("button", name=re.compile(pat, re.I))
            try:
                if await cand.count() > 0 and await cand.first.is_visible():
                    await cand.first.click()
                    log.info("Clicked hangup candidate pattern %s.", pat)
                    hung = True
                    break
            except Exception as e:  # noqa: BLE001
                log.info("hangup click %s failed: %r", pat, e)
        if not hung:
            log.info("No hangup button matched — please hang up manually, leaving browser open 20s.")
            await asyncio.sleep(20)
        await dump_all(ctx, "after_hangup")
        await ctx.close()
        if dev is not None:
            await dev.teardown()


async def mode_watch(seconds: int = 150) -> None:
    async with async_playwright() as pw:
        ctx = await launch_browser(pw)

        def on_page(page):
            log.info("NEW PAGE/popup detected! url=%s", page.url)

        ctx.on("page", on_page)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if "whatsapp" not in page.url:
            await page.goto("https://web.whatsapp.com")
        log.info("Watching for %ds. Place a VOICE test call NOW from your second phone.", seconds)
        log.info("Dumping every 1s from every page. Call UI may be same page / popup / new window.")
        seen: set[str] = set()
        for tick in range(seconds):
            for p in ctx.pages:
                key = f"{p.url}"
                if key not in seen:
                    log.info("tick %d: page url=%s", tick, p.url)
                    seen.add(key)
            await dump_all(ctx, f"ring_{tick:02d}")
            await asyncio.sleep(1)
        log.info("Watch done. Inspect data/debug/ring_*.html/png to find the ring UI.")
        await ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["login", "watch", "accept"], default="login")
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--audio-test", action="store_true", help="accept mode: record both tracks for 20s")
    ap.add_argument("--play-wav", default=None, help="accept+audio-test: wav to play into the call")
    args = ap.parse_args()
    if args.mode == "login":
        asyncio.run(mode_login())
    elif args.mode == "accept":
        asyncio.run(mode_accept(record_secs=20 if args.audio_test else 0, play_wav=args.play_wav))
    else:
        asyncio.run(mode_watch(args.seconds))


if __name__ == "__main__":
    main()
