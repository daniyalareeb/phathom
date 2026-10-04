"""Call-end detection: the call screen can linger after hang-up, so a frozen timer = ended."""

from phathom.whatsapp import driver as drv


class Clock:
    t = 1000.0

    def monotonic(self) -> float:
        return self.t


def make_driver(monkeypatch, screen: dict):
    d = drv.WhatsAppDriver()

    async def first_visible(factory):
        return object() if screen["end_call"] else None

    async def timer():
        return screen["timer"]

    monkeypatch.setattr(d, "_first_visible", first_visible)
    monkeypatch.setattr(d, "_call_timer", timer)
    clock = Clock()
    monkeypatch.setattr(drv.time, "monotonic", clock.monotonic)
    return d, clock


async def test_no_end_call_button_means_not_active(monkeypatch):
    d, _ = make_driver(monkeypatch, {"end_call": False, "timer": None})
    assert not await d.is_call_active()


async def test_ringing_outgoing_call_without_timer_is_active(monkeypatch):
    d, clock = make_driver(monkeypatch, {"end_call": True, "timer": None})
    assert await d.is_call_active()
    clock.t += 30
    assert await d.is_call_active()


async def test_ticking_timer_is_active_frozen_timer_is_ended(monkeypatch):
    screen = {"end_call": True, "timer": "0:09"}
    d, clock = make_driver(monkeypatch, screen)
    assert await d.is_call_active()
    clock.t += 1
    screen["timer"] = "0:10"
    assert await d.is_call_active()
    clock.t += 2  # same text, only 2s -> still active
    assert await d.is_call_active()
    clock.t += drv.TIMER_STALE_S  # screen lingers, timer frozen at 0:10
    assert not await d.is_call_active()
