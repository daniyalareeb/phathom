"""Contacts tests — policy matching per SPEC §6."""

from phathom.contacts import notes_for, should_answer

CONTACTS = {
    "policy": "all",
    "block": ["Unknown"],
    "allow": [],
    "notes": {"Ammi": "Mother. Be warm, speak Urdu."},
}


def test_off_answers_nothing():
    assert should_answer("Ammi", "off", CONTACTS) is False


def test_block_list():
    assert should_answer("Unknown", "notes", CONTACTS) is False
    assert should_answer("unknown", "assistant", CONTACTS) is False
    assert should_answer("Ammi", "notes", CONTACTS) is True


def test_allowlist_policy():
    c = dict(CONTACTS, policy="allowlist", allow=["Ammi"])
    assert should_answer("Ammi", "notes", c) is True
    assert should_answer("Ahmed Uni", "notes", c) is False
    assert should_answer("Unknown", "notes", c) is False


def test_tilde_prefix_ignored():
    assert should_answer("~Ftm studios", "notes",
                         dict(CONTACTS, block=["Ftm studios"])) is False


def test_notes_lookup():
    assert notes_for("Ammi", CONTACTS) == "Mother. Be warm, speak Urdu."
    assert notes_for("~ammi", CONTACTS) == "Mother. Be warm, speak Urdu."
    assert notes_for("Nobody", CONTACTS) is None
