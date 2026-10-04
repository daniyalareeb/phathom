"""decide() — pure policy for the EXTENSION_SPEC §3 table."""

import pytest

from phathom.server.call_session import decide


@pytest.mark.parametrize("mode", ["listen", "talk"])
@pytest.mark.parametrize("event", ["ring"])
def test_ring_answered_when_connected_allowed(event, mode):
    assert decide(event, connected=True, answer_mode=mode,
                  copilot=True, blocked=False) == "answer_after_delay"


def test_ring_ignored_when_disconnected():
    assert decide("ring", connected=False, answer_mode="talk",
                  copilot=True, blocked=False) == "ignore"


def test_ring_ignored_when_answer_off():
    assert decide("ring", connected=True, answer_mode="off",
                  copilot=True, blocked=False) == "ignore"


@pytest.mark.parametrize("mode", ["listen", "talk"])
def test_ring_ignored_when_blocked(mode):
    assert decide("ring", connected=True, answer_mode=mode,
                  copilot=True, blocked=True) == "ignore"


@pytest.mark.parametrize("event", ["owner_answered", "outgoing"])
def test_owner_calls_recorded_with_copilot(event):
    assert decide(event, connected=True, answer_mode="talk",
                  copilot=True, blocked=False) == "record_copilot"


@pytest.mark.parametrize("event", ["owner_answered", "outgoing"])
def test_owner_calls_ignored_without_copilot(event):
    assert decide(event, connected=True, answer_mode="talk",
                  copilot=False, blocked=False) == "nothing"


@pytest.mark.parametrize("event", ["owner_answered", "outgoing"])
def test_owner_calls_ignored_when_blocked(event):
    assert decide(event, connected=True, answer_mode="talk",
                  copilot=True, blocked=True) == "nothing"


@pytest.mark.parametrize("event", ["owner_answered", "outgoing"])
def test_owner_calls_ignored_when_disconnected(event):
    assert decide(event, connected=False, answer_mode="talk",
                  copilot=True, blocked=False) == "nothing"


def test_copilot_records_in_every_mode():
    # Copilot works in every mode, including off (SPEC §12b).
    for mode in ("off", "listen", "talk"):
        assert decide("outgoing", connected=True, answer_mode=mode,
                      copilot=True, blocked=False) == "record_copilot"


def test_unknown_event_raises():
    with pytest.raises(ValueError):
        decide("fax", connected=True, answer_mode="talk", copilot=True, blocked=False)
