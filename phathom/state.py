"""Daemon state file — SPEC §7.1. Atomic writes; mode re-read on every call."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("phathom.state")

STATE_PATH = Path("data/state.json")

DEFAULTS = {
    "mode": "assistant",
    "copilot": True,  # take notes on calls the owner handles himself in the WhatsApp window
    "connected": False,  # v2 Connect switch (EXTENSION_SPEC §3)
    "daemon_pid": None,
    "started_at": None,
    "current_call": None,
    "wa_logged_in": None,
}


def read() -> dict:
    state = dict(DEFAULTS)
    try:
        if STATE_PATH.exists():
            state.update(json.loads(STATE_PATH.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state read failed (%r); using defaults", e)
    if not state.get("mode"):
        from phathom.config import settings
        state["mode"] = settings.DEFAULT_MODE
    return state


def write(patch: dict) -> dict:
    state = read()
    state.update(patch)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return state


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OverflowError):
        return False
    return True
