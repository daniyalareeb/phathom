"""Caller-source auto-pick tests (EXTENSION_SPEC §5.7, step B2).

Synthetic 5 s VAD streams (50 x 100 ms buckets):
- silent keep-alive tap: never voiced;
- mic-echo tap: voiced only when the owner is voiced (must be rejected);
- caller tap: voiced while the owner is silent (must win).
"""

from phathom.server import caller_pick
from phathom.server.caller_pick import pick_caller, resolve_source, score_taps


def _buckets() -> list[dict[str, int]]:
    buckets: list[dict[str, int]] = []
    for i in range(50):
        owner = 2 if 10 <= i < 20 else 0  # owner speaks in buckets 10..19
        caller = 0 if 10 <= i < 20 else (2 if i % 2 == 0 else 1)  # caller pauses for owner
        echo = owner  # mic echo mirrors the owner exactly
        buckets.append({"owner": owner, "speaker:1(keepalive)": 0,
                        "speaker:2(echo)": echo, "speaker:3(caller)": caller})
    return buckets


def test_scores_reject_echo_and_silence():
    scores = score_taps(_buckets())
    assert scores.get("speaker:1(keepalive)", 0) == 0
    assert scores.get("speaker:2(echo)", 0) == 0
    assert scores["speaker:3(caller)"] > 0


def test_pick_caller_selects_caller_tap():
    assert pick_caller(_buckets()) == "speaker:3(caller)"


def test_pick_caller_none_when_silent():
    buckets = [{"owner": 0, "speaker:1(x)": 0} for _ in range(50)]
    assert pick_caller(buckets) is None
    assert pick_caller([]) is None


def test_resolve_auto_order():
    b = _buckets()
    assert resolve_source(mode="auto", webrtc_seen=True, tab_seen=True, buckets=b) == "webrtc"
    assert resolve_source(mode="auto", webrtc_seen=False, tab_seen=True, buckets=b) == "tab"
    assert (resolve_source(mode="auto", webrtc_seen=False, tab_seen=False, buckets=b)
            == "speaker:speaker:3(caller)")
    assert resolve_source(mode="auto", webrtc_seen=False, tab_seen=False, buckets=[]) is None


def test_resolve_explicit_modes():
    b = _buckets()
    assert resolve_source(mode="webrtc", webrtc_seen=True, tab_seen=False, buckets=b) == "webrtc"
    assert resolve_source(mode="webrtc", webrtc_seen=False, tab_seen=True, buckets=b) is None
    assert resolve_source(mode="tab_capture", webrtc_seen=False, tab_seen=True, buckets=b) == "tab"
    assert (resolve_source(mode="speaker_tap", webrtc_seen=True, tab_seen=True, buckets=b)
            == "speaker:speaker:3(caller)")


def test_kind_tab_mapping():
    from phathom.server.ws import TRACK_OF_KIND
    from phathom.server.ws_audio import KIND_BOT, KIND_CALLER, KIND_OWNER, KIND_TAB
    assert KIND_TAB == 0x04
    assert TRACK_OF_KIND[KIND_TAB] == "tab"
    assert TRACK_OF_KIND[KIND_CALLER] == "caller"
    assert TRACK_OF_KIND[KIND_OWNER] == "owner"
    assert KIND_BOT == 0x11


def test_caller_source_setting_validation():
    import pytest
    from phathom.server import runtime_settings
    with pytest.raises(KeyError):
        runtime_settings.set_many({"caller_source": "pulseaudio"})
