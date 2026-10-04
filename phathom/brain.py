"""Brain (§8): knowledge from WhatsApp chats.

Two ingestion paths: exported-chat import (reliable, first) and the Web reader
(convenience). Messages dedup on sha1(ts + sender + text); brains build by
extracting per-window JSON with the cheap model then merging with the smart
one. Every fact keeps a source {message_ts} for citations.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("phathom.brain")

# Android: 12/05/2026, 21:34 - Ahmed: hello  (also . and / and - separators)
ANDROID_RE = re.compile(
    r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4}),\s+(\d{1,2}):(\d{2})(?:\s*([AaPp])\.?[Mm]\.?)?\s*[-–]\s*(.*?):\s?(.*)$")
# iOS: [12/05/2026, 21:34:05] Ahmed: hello
IOS_RE = re.compile(
    r"^\[(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4}),\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?\]\s*(.*?):\s?(.*)$")

SYSTEM_HINTS = ("messages and calls are end-to-end encrypted", "created group",
                "added you", "changed the subject", "you were added")


def _to_iso(day: str, mon: str, year: str, hh: str, mm: str,
            ampm: str | None) -> str | None:
    try:
        y = int(year)
        y += 2000 if y < 100 else 0
        h = int(hh)
        if ampm:
            a = ampm.lower().replace(".", "")
            if a.startswith("p") and h < 12:
                h += 12
            if a.startswith("a") and h == 12:
                h = 0
        return datetime(y, int(mon), int(day), h, int(mm),
                        tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def parse_export(text: str, me_names: tuple[str, ...] = ()) -> list[dict]:
    """Parse an exported chat (.txt) into [{ts, sender, is_me, text}].

    Handles Android + iOS formats, 12/24 h, multi-line messages; system lines
    (no sender colon match or encryption notices) are skipped. Voice notes
    appear as their caption text; unparseable media lines are kept as-is when
    they follow a message (multi-line continuation).
    """
    messages: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current and (current["text"] or "").strip():
            current["text"] = current["text"].strip()
            messages.append(current)
        current = None

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = ANDROID_RE.match(line) or IOS_RE.match(line)
        if m:
            flush()
            g = m.groups()
            if len(g) == 8:  # android: d,mo,y,h,mi,ampm,sender,text
                d, mo, y, h, mi, ampm, sender, t = g
            else:  # ios: d,mo,y,h,mi,ss,ampm,sender,text
                d, mo, y, h, mi, _ss, ampm, sender, t = g
            low = (sender or "").strip().lower()
            if any(hint in (t or "").lower() for hint in SYSTEM_HINTS) and not (t or "").strip():
                current = None
                continue
            iso = _to_iso(d, mo, y, h, mi, ampm)
            if not iso:
                current = None
                continue
            sender = (sender or "").strip()
            if not sender:
                current = None
                continue
            current = {"ts": iso, "sender": sender,
                       "is_me": sender in me_names, "text": t or ""}
        else:
            if current is not None and line.strip():
                if any(hint in line.lower() for hint in SYSTEM_HINTS):
                    continue  # encryption/group notices are system lines, not chat
                current["text"] += "\n" + line.strip()
            # stray lines outside any message are dropped
    flush()
    return messages


def read_export_file(path: Path) -> str:
    """Read a .txt export, or the first .txt inside a .zip export."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txts:
                raise ValueError("zip has no .txt chat export")
            return z.read(txts[0]).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def msg_hash(ts: str, sender: str, text: str) -> str:
    return hashlib.sha1(f"{ts}\x00{sender}\x00{text}".encode("utf-8")).hexdigest()


def chat_slug(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in (name or ""))
    out = "_".join(p for p in out.split("_") if p)
    return out[:48] or "chat"


def with_hashes(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        m = dict(m)
        m.setdefault("hash", msg_hash(m.get("ts", ""), m.get("sender", ""), m.get("text", "")))
        out.append(m)
    return out


def _window(messages: list[dict], max_chars: int = 12000) -> list[str]:
    chunks, cur = [], []
    size = 0
    for m in messages:
        line = f"[{m.get('ts', '')[:10]}] {m.get('sender', '')}: {m.get('text', '')}"
        if size + len(line) > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


EXTRACT_PROMPT = """Extract durable knowledge from these WhatsApp messages. Reply ONLY with JSON:
{{"facts": [{{"text": str, "message_ts": str}}], "open_loops": [{{"text": str, "message_ts": str}}],
"decisions": [{{"text": str, "message_ts": str}}], "people": [{{"name": str, "note": str}}],
"timeline_events": [{{"text": str, "message_ts": str}}]}}
Keep each list short (<=10 items). message_ts is the [date] of the source message.
Messages:
{WINDOW}"""

MERGE_PROMPT = """Merge these newly extracted items into the existing brain. Dedupe, resolve contradictions
(newest wins), keep each list <= 30 items, summary <= 120 words. Reply ONLY with JSON:
{{"summary": str, "facts": [...], "open_loops": [...], "decisions": [...], "people": [...], "timeline": [...]}}
Keep the same item shapes. Existing brain:
{BRAIN}
New items:
{NEW}"""


async def _extract_window(window: str) -> dict:
    from phathom.ai import llm as llm_mod
    from phathom.config import settings
    data = await llm_mod._chat_json(
        settings.LLM_MODEL_LIVE,
        [{"role": "user", "content": EXTRACT_PROMPT.format(WINDOW=window[:12000])}],
        0.2, 800, None)
    return data


async def _merge(existing: dict, new_items: dict) -> dict:
    from phathom.ai import llm as llm_mod
    from phathom.config import settings
    import json as _json
    fallback = settings.LLM_MODEL_LIVE if settings.LLM_MODEL_LIVE != settings.LLM_MODEL_SMART else None
    data = await llm_mod._chat_json(
        settings.LLM_MODEL_SMART,
        [{"role": "user", "content": MERGE_PROMPT.format(
            BRAIN=_json.dumps(existing)[:6000], NEW=_json.dumps(new_items)[:6000])}],
        0.2, 1200, fallback)
    return data


def merge_local(existing: dict, new_items: dict) -> dict:
    """Deterministic merge (used by tests and as the offline fallback): concat +
    dedupe by text, cap 30, newest message_ts wins for ordering."""
    merged = dict(existing)
    for key in ("facts", "open_loops", "decisions", "timeline_events", "timeline"):
        seen: dict[str, dict] = {}
        for item in list(existing.get(key, [])) + list(new_items.get(key, [])):
            if isinstance(item, dict) and item.get("text"):
                seen[item["text"].strip()] = item
            elif isinstance(item, str) and item.strip():
                seen[item.strip()] = {"text": item.strip()}
        merged[key] = list(seen.values())[-30:]
    people: dict[str, dict] = {}
    for p in list(existing.get("people", [])) + list(new_items.get("people", [])):
        if isinstance(p, dict) and p.get("name"):
            people[p["name"].strip()] = p
    merged["people"] = list(people.values())[-30:]
    return merged


async def rebuild_brain(store, chat_id: str, progress=None) -> dict:
    """Rebuild a chat's brain from messages after brain.last_msg_ts (§8.3).

    Sequential windows (rate-limit friendly); LLM merge with local fallback.
    Returns the saved brain dict.
    """
    chat = await store.get_chat(chat_id)
    if not chat:
        raise ValueError(f"no such chat: {chat_id}")
    old = await store.get_brain(chat_id) or {}
    since = old.get("last_msg_ts")
    messages = await store.get_messages_since(chat_id, since)
    if not messages:
        return old
    windows = _window(messages)
    extracted: dict = {}
    for i, w in enumerate(windows):
        if progress:
            try:
                progress(i + 1, len(windows))
            except TypeError:
                progress(f"{i + 1}/{len(windows)}")
        try:
            part = await _extract_window(w)
            extracted = merge_local(extracted, part)
        except Exception as e:  # noqa: BLE001 — 429/backoff: keep going, merge later
            log.warning("brain extract window %d/%d failed: %r", i + 1, len(windows), e)
    try:
        merged = await _merge(old, extracted)
    except Exception as e:  # noqa: BLE001
        log.warning("brain LLM merge failed, using local merge: %r", e)
        merged = merge_local(old, extracted)
        merged["summary"] = old.get("summary") or f"{len(messages)} messages about {chat.get('name')}."
    merged["last_msg_ts"] = messages[-1]["ts"]
    await store.save_brain(chat_id, merged)
    return merged


async def brain_context_for(store, peer: str, limit: int = 1500) -> str:
    """Short brain summary for LiveAgent contact_notes (§8.4, capped)."""
    if not peer or peer == "Unknown":
        return ""
    try:
        chats = await store.list_chats()
    except Exception:  # noqa: BLE001
        return ""
    want = peer.strip().lower()
    match = next((c for c in chats if str(c.get("name", "")).strip().lower() == want), None)
    if not match:
        return ""
    cid = str(match.get("id", "")).removeprefix("chat:")
    try:
        brain = await store.get_brain(cid)
    except Exception:  # noqa: BLE001
        return ""
    if not brain or not brain.get("summary"):
        return ""
    loops = "; ".join(x.get("text", "") for x in (brain.get("open_loops", []) or [])[:3]
                       if isinstance(x, dict))
    text = str(brain["summary"])
    if loops:
        text += f" Open loops: {loops}"
    return text[:limit]
