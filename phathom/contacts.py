"""Contact policy — SPEC §6. Case-insensitive exact match on the WhatsApp caller label."""

import logging
from pathlib import Path

import yaml

log = logging.getLogger("phathom.contacts")


def load(path: str | Path = "contacts.yaml") -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        return {"policy": "all", "block": [], "allow": [], "notes": {}}
    except yaml.YAMLError as e:
        log.warning("contacts.yaml parse error (%r); using defaults", e)
        return {"policy": "all", "block": [], "allow": [], "notes": {}}
    data.setdefault("policy", "all")
    data.setdefault("block", [])
    data.setdefault("allow", [])
    data.setdefault("notes", {})
    return data


def _match(label: str, entries: list) -> bool:
    want = (label or "").strip().lower().lstrip("~").strip()
    return any(str(e).strip().lower().lstrip("~").strip() == want for e in entries)


def should_answer(caller_label: str, mode: str, contacts: dict | None = None) -> bool:
    if mode == "off":
        return False
    contacts = contacts if contacts is not None else load()
    if _match(caller_label, contacts.get("block", [])):
        return False
    if contacts.get("policy") == "allowlist":
        return _match(caller_label, contacts.get("allow", []))
    return True


def notes_for(caller_label: str, contacts: dict | None = None) -> str | None:
    contacts = contacts if contacts is not None else load()
    notes = contacts.get("notes", {}) or {}
    want = (caller_label or "").strip().lower().lstrip("~").strip()
    for name, note in notes.items():
        if str(name).strip().lower().lstrip("~").strip() == want:
            return note
    return None
