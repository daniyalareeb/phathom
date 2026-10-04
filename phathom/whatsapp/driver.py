"""WhatsApp Web driver — SPEC §9.2. All selectors come from selectors.py."""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, Locator, Page, Playwright

from phathom.config import settings
from phathom.whatsapp import selectors as S

log = logging.getLogger("phathom.whatsapp")

# The call screen can stay up after a call ends (data/debug/*_after_hangup still showed
# "End call"), so a timer that stops ticking for this long also counts as ended.
TIMER_STALE_S = 4.0


async def launch_browser(pw: Playwright, headed: bool = True) -> BrowserContext:
    """Launch the persistent Chrome profile with Phathom audio routing.

    PULSE_SINK/PULSE_SOURCE aim Chrome at the virtual devices (§2.2).
    Do NOT pass --use-fake-device-for-media-stream (it replaces the mic).
    """
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="data/chrome-profile",
        channel=settings.CHROME_CHANNEL,
        headless=not headed,  # WebRTC calls need a real (headed) browser
        env={**os.environ,
             "PULSE_SINK": "phathom_speaker", "PULSE_SOURCE": "phathom_mic"},
        permissions=["microphone", "notifications"],
        args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"],
        viewport={"width": 1280, "height": 800},
    )
    await ctx.grant_permissions(["microphone"], origin="https://web.whatsapp.com")
    return ctx


@dataclass
class IncomingCall:
    caller: str           # label shown in the ring UI (contact name or phone number)
    is_video: bool
    is_group: bool


def _apply(page: Page, spec: tuple[str, str]) -> Locator:
    strategy, value = spec
    if not value:
        raise ValueError(f"selector not discovered yet: {spec}")
    if strategy == "role":
        return page.get_by_role("button", name=re.compile(value, re.I))
    if strategy == "css":
        return page.locator(value)
    if strategy == "text":
        return page.get_by_text(re.compile(value, re.I))
    raise ValueError(f"unknown selector strategy: {strategy}")


class WhatsAppDriver:
    def __init__(self):
        self.ctx: BrowserContext | None = None
        self._pw = None
        self._timer_text: str | None = None
        self._timer_changed_at = 0.0

    async def start(self, headed: bool = True) -> None:
        from playwright.async_api import async_playwright
        self._pw = async_playwright()
        pw = await self._pw.__aenter__()
        self.ctx = await launch_browser(pw, headed=headed)
        self.ctx.on("page", lambda p: log.info("new page/popup: %s", p.url))
        page = await self._main_page()
        if "whatsapp" not in page.url:
            await page.goto("https://web.whatsapp.com")

    async def _main_page(self) -> Page:
        assert self.ctx is not None
        return self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()

    def _pages(self) -> list[Page]:
        return list(self.ctx.pages) if self.ctx else []

    async def _first_visible(self, factory) -> Locator | None:
        """First visible locator across all open pages (call UI may move)."""
        for page in self._pages():
            try:
                loc = factory(page)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return loc.first
            except ValueError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("locator check failed: %r", e)
        return None

    async def is_logged_in(self) -> bool:
        try:
            loc = await self._first_visible(lambda p: _apply(p, S.LOGGED_IN_MARKER))
            return loc is not None
        except ValueError:
            return False

    async def wait_for_login(self, timeout_s: int = 300) -> None:
        for _ in range(timeout_s * 2):
            if await self.is_logged_in():
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("WhatsApp login not detected within timeout")

    async def poll_incoming_call(self) -> IncomingCall | None:
        """Non-blocking: an Accept button visible on any page means a call is ringing."""
        try:
            accept = await self._first_visible(lambda p: _apply(p, S.ACCEPT_BUTTON))
        except ValueError:
            return None
        if accept is None:
            return None
        caller = "Unknown"
        try:
            name_loc = await self._first_visible(lambda p: _apply(p, S.INCOMING_CALLER))
            if name_loc is not None:
                caller = (await name_loc.inner_text()).strip().lstrip("~").strip() or "Unknown"
        except ValueError:
            pass
        is_video = False
        try:
            video_loc = await self._first_visible(lambda p: _apply(p, S.INCOMING_IS_VIDEO))
            is_video = video_loc is not None
        except ValueError:
            pass
        # v1 has no group-call marker: group calls are out of scope (§13).
        return IncomingCall(caller=caller, is_video=is_video, is_group=False)

    async def accept(self, audio_only: bool = True) -> None:
        try:
            accept = await self._first_visible(lambda p: _apply(p, S.ACCEPT_BUTTON))
        except ValueError as e:
            await self.debug_dump("accept_error")
            raise RuntimeError(f"cannot accept: {e}")
        if accept is None:
            await self.debug_dump("accept_error")
            raise RuntimeError("accept: no visible Accept button")
        await accept.click()
        self._timer_text, self._timer_changed_at = None, time.monotonic()
        log.info("clicked Accept (audio_only=%s)", audio_only)
        if audio_only:
            # Video calls: keep the camera off (v1 never sends video).
            try:
                cam = await self._first_visible(lambda p: _apply(p, S.CAMERA_TOGGLE))
                if cam is not None:
                    log.info("camera toggle visible; leaving camera off (not clicking)")
            except ValueError:
                pass
            except Exception as e:  # noqa: BLE001
                log.debug("camera check: %r", e)

    async def is_call_active(self) -> bool:
        """Connected = "End call" visible AND the timer (if shown) still ticking.
        Before connecting (outgoing "Ringing...") there is no timer, so the button decides."""
        try:
            hangup = await self._first_visible(lambda p: _apply(p, S.HANGUP_BUTTON))
        except ValueError:
            return False
        now = time.monotonic()
        if hangup is None:
            self._timer_text, self._timer_changed_at = None, now
            return False
        text = await self._call_timer()
        if text is None:
            self._timer_changed_at = now
            return True
        if text != self._timer_text:
            self._timer_text, self._timer_changed_at = text, now
            return True
        return now - self._timer_changed_at < TIMER_STALE_S

    async def _call_timer(self) -> str | None:
        try:
            loc = await self._first_visible(lambda p: _apply(p, S.CALL_TIMER))
            return (await loc.inner_text()).strip() if loc is not None else None
        except Exception as e:  # noqa: BLE001
            log.debug("timer read: %r", e)
            return None

    async def current_peer_name(self) -> str | None:
        """Name of the other person on the current call (same element as the ring UI)."""
        try:
            loc = await self._first_visible(lambda p: _apply(p, S.INCOMING_CALLER))
            if loc is not None:
                return (await loc.inner_text()).strip().lstrip("~").strip() or None
        except Exception as e:  # noqa: BLE001
            log.debug("peer name: %r", e)
        return None

    async def hang_up(self) -> None:
        try:
            hangup = await self._first_visible(lambda p: _apply(p, S.HANGUP_BUTTON))
        except ValueError as e:
            await self.debug_dump("hangup_error")
            raise RuntimeError(f"cannot hang up: {e}")
        if hangup is None:
            log.info("hang_up: no hangup button visible; already ended")
            return
        await hangup.click()
        log.info("clicked hang-up")
        for _ in range(10):  # confirm within 5s; one retry, then leave evidence
            await asyncio.sleep(0.5)
            if not await self.is_call_active():
                return
        log.warning("call still looks active 5s after hang-up; clicking again")
        await self.debug_dump("hangup_stuck")
        again = await self._first_visible(lambda p: _apply(p, S.HANGUP_BUTTON))
        if again is not None:
            await again.click()

    async def debug_dump(self, tag: str) -> Path:
        outdir = Path("data/debug")
        outdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for i, page in enumerate(self._pages()):
            try:
                await page.screenshot(path=str(outdir / f"{ts}_{tag}_p{i}.png"))
                (outdir / f"{ts}_{tag}_p{i}.html").write_text(await page.content(), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                log.warning("debug_dump page %d failed: %r", i, e)
        log.info("debug dump: %s_%s", ts, tag)
        return outdir / f"{ts}_{tag}_p0.png"

    async def close(self) -> None:
        if self.ctx is not None:
            try:
                await self.ctx.close()
            except Exception as e:  # noqa: BLE001
                log.warning("browser close: %r", e)
            self.ctx = None
        if self._pw is not None:
            try:
                await self._pw.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                log.warning("playwright stop: %r", e)
            self._pw = None
