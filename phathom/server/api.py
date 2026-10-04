"""REST API (/api, all bearer-authenticated) — EXTENSION_SPEC §6.5."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from phathom import contacts as contacts_mod
from phathom import pipeline as pipeline_mod
from phathom import state as state_mod
from phathom.ai.llm import answer_question
from phathom.config import settings
from phathom.server import runtime_settings
from phathom.server.auth import bearer_from_header, verify_token
from phathom.server.call_session import ANSWER_OF
from phathom.store import Store, make_call_id

log = logging.getLogger("phathom.server.api")

router = APIRouter(prefix="/api")

DONE_PATH = Path("data/action_done.json")


async def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not verify_token(bearer_from_header(authorization)):
        raise HTTPException(status_code=401, detail="bad or missing token")


def _short(call: dict) -> dict:
    cid = str(call.get("id", "")).removeprefix("call:")
    return {"id": cid, "caller": call.get("caller"), "started_at": call.get("started_at"),
            "ended_at": call.get("ended_at"), "duration_s": call.get("duration_s"),
            "mode": call.get("mode"), "status": call.get("status"), "title": call.get("title"),
            "summary": call.get("summary"),
            "has_message": bool(call.get("message_for_owner")),
            "message": call.get("message_for_owner"),
            "message_read": bool(call.get("message_read", False)),
            "starred": bool(call.get("starred", False))}


@router.get("/health", dependencies=[Depends(require_auth)])
async def health():
    try:
        store = await Store().connect()
        try:
            await store.query("RETURN 1")
            db = "up"
        finally:
            await store.close()
    except Exception as e:  # noqa: BLE001
        db = f"down: {e!r}"[:200]
    st = state_mod.read()
    return {"server": "phathom", "version": "0.2.0", "db": db,
            "groq_key": "set" if settings.GROQ_API_KEY else "missing",
            "wa_logged_in": st.get("wa_logged_in"),
            "connected": bool(st.get("connected", False))}


@router.get("/state", dependencies=[Depends(require_auth)])
async def get_state():
    st = state_mod.read()
    mode = st.get("mode", "assistant")
    return {"connected": bool(st.get("connected", False)), "mode": mode,
            "answer": ANSWER_OF.get(mode, "off"), "copilot": bool(st.get("copilot", True)),
            "answer_delay_s": runtime_settings.get("answer_delay_s"),
            "live_transcript": runtime_settings.get("live_transcript"),
            "side_panel_auto": runtime_settings.get("side_panel_auto"),
            "wa_logged_in": st.get("wa_logged_in"),
            "current_call": st.get("current_call")}


@router.patch("/state", dependencies=[Depends(require_auth)])
async def patch_state(patch: dict, request: Request):
    allowed_top = {"connected", "copilot"}
    updates: dict = {}
    for k in allowed_top:
        if k in patch:
            updates[k] = bool(patch[k])
    if "mode" in patch:
        ui_mode = patch["mode"]
        # Accept both UI values (off/listen/talk) and CLI values (off/notes/assistant).
        mapping = {"off": "off", "listen": "notes", "talk": "assistant",
                   "notes": "notes", "assistant": "assistant"}
        if ui_mode not in mapping:
            raise HTTPException(status_code=422, detail="mode must be off|listen|talk")
        updates["mode"] = mapping[ui_mode]
    sub = {k: patch[k] for k in ("answer_delay_s", "live_transcript", "side_panel_auto")
           if k in patch}
    if sub:
        try:
            runtime_settings.set_many(sub)
        except KeyError as e:
            raise HTTPException(status_code=422, detail=f"bad setting: {e}")
    if updates:
        state_mod.write(updates)
    # Disconnect mid-call ends bot calls gracefully; copilot just stops (B3).
    if patch.get("connected") is False:
        manager = getattr(request.app.state, "manager", None)
        if manager is not None:
            try:
                await manager.disconnect_gracefully()
            except Exception as e:  # noqa: BLE001
                log.warning("graceful disconnect failed: %r", e)
    return await get_state()


@router.get("/calls", dependencies=[Depends(require_auth)])
async def list_calls(q: str | None = None, mode: str | None = None,
                     contact: str | None = None, from_: str | None = None,
                     to: str | None = None, cursor: str | None = None,
                     limit: int = 20):
    limit = max(1, min(limit, 100))
    store = await Store().connect()
    try:
        ids: list[str] | None = None
        if q:
            segs = await store.query(
                "SELECT call FROM segment WHERE text @1@ $q LIMIT 200", {"q": q}) or []
            calls = await store.query(
                "SELECT id FROM call WHERE summary @1@ $q LIMIT 50", {"q": q}) or []
            seen: list[str] = []
            for r in (segs or []) + (calls or []):
                cid = str(r.get("call", r.get("id", ""))).removeprefix("call:")
                if cid and cid not in seen:
                    seen.append(cid)
            ids = seen
        conds, vars_ = [], {}
        if ids is not None:
            if not ids:
                return {"calls": [], "next_cursor": None}
            conds.append("string::contains(id, $frag)")
            # SurrealDB: filter in python for id lists (small result sets in v2).
        if mode:
            conds.append("mode = $mode")
            vars_["mode"] = mode
        if contact:
            conds.append("caller = $contact")
            vars_["contact"] = contact
        if from_:
            conds.append("started_at >= type::datetime($from)")
            vars_["from"] = from_
        if to:
            conds.append("started_at <= type::datetime($to)")
            vars_["to"] = to
        if cursor:
            conds.append("started_at < type::datetime($cursor)")
            vars_["cursor"] = cursor
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        rows = await store.query(
            "SELECT id, caller, started_at, ended_at, duration_s, mode, status, title,"
            " summary, message_for_owner, message_read, starred FROM call "
            f"{where} ORDER BY started_at DESC LIMIT $limit", {**vars_, "limit": limit + 1})
        rows = rows or []
        if ids is not None:
            rows = [r for r in rows
                    if str(r.get("id", "")).removeprefix("call:") in set(ids)][:limit + 1]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = str(rows[limit].get("started_at", ""))
            rows = rows[:limit]
        return {"calls": [_short(r) for r in rows], "next_cursor": next_cursor}
    finally:
        await store.close()


@router.get("/calls/{call_id}", dependencies=[Depends(require_auth)])
async def get_call(call_id: str):
    store = await Store().connect()
    try:
        call = await store.get_call(call_id)
        if not call:
            raise HTTPException(status_code=404, detail="no such call")
        segs = await store.get_segments(call_id)
        full = _short(call)
        audio = {t: f"/api/calls/{full['id']}/audio/{t}"
                 for t in ("mix", "caller", "owner", "bot", "speaker")}
        # STEP 1A: expose per-tap wavs (speaker:N) recorded for diagnosis.
        try:
            adir = Path(call.get("audio_dir") or "")
            for wav in sorted(adir.glob("*.wav")) + sorted((adir / "taps").glob("*.wav")):
                stem = wav.stem
                if stem not in ("caller", "owner", "bot", "speaker") and stem not in audio:
                    audio[stem] = f"/api/calls/{full['id']}/audio/{stem}"
        except Exception:  # noqa: BLE001
            pass
        full.update({"summary": call.get("summary"), "language": call.get("language"),
                     "key_points": call.get("key_points", []),
                     "action_items": call.get("action_items", []),
                     "message_for_owner": call.get("message_for_owner"),
                     "message_read": bool(call.get("message_read", False)),
                     "follow_up_needed": bool(call.get("follow_up_needed", False)),
                     "notes": call.get("notes"), "audio_dir": call.get("audio_dir"),
                     "segments": segs, "audio": audio})
        return full
    finally:
        await store.close()


@router.get("/calls/{call_id}/audio/{track}", dependencies=[Depends(require_auth)])
async def get_audio(call_id: str, track: str):
    names = {"caller": "caller.wav", "owner": "owner.wav", "bot": "bot.wav",
             "speaker": "speaker.wav", "mix": "mixed.mp3"}
    if track in names:
        filename = names[track]
    elif re.fullmatch(r"[A-Za-z0-9_]+", track):
        # STEP 1A per-tap wavs (speaker_1_...).
        filename = track + ".wav"
    else:
        raise HTTPException(status_code=404, detail="unknown track")
    store = await Store().connect()
    try:
        call = await store.get_call(call_id)
        if not call or not call.get("audio_dir"):
            raise HTTPException(status_code=404, detail="no audio")
        adir = Path(call["audio_dir"])
        path = adir / filename
        if not path.exists():
            path = adir / "taps" / filename  # §5.7 per-tap wavs
        if not path.exists():
            raise HTTPException(status_code=404, detail="track missing")
        media = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
        return FileResponse(path, media_type=media)
    finally:
        await store.close()


@router.patch("/calls/{call_id}", dependencies=[Depends(require_auth)])
async def patch_call(call_id: str, patch: dict):
    fields = {k: patch[k] for k in ("title", "starred", "notes", "message_read") if k in patch}
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to update")
    store = await Store().connect()
    try:
        if not await store.get_call(call_id):
            raise HTTPException(status_code=404, detail="no such call")
        await store.update_call(call_id, fields)
        return _short(await store.get_call(call_id))
    finally:
        await store.close()


@router.delete("/calls/{call_id}", dependencies=[Depends(require_auth)])
async def delete_call(call_id: str):
    store = await Store().connect()
    try:
        call = await store.get_call(call_id)
        if not call:
            raise HTTPException(status_code=404, detail="no such call")
        await store.delete_segments(call_id)
        await store.query("DELETE call WHERE id = $id", {"id": f"call:{call_id}"})
        if call.get("audio_dir"):
            shutil.rmtree(call["audio_dir"], ignore_errors=True)
        return {"ok": True}
    finally:
        await store.close()


@router.post("/calls/{call_id}/reprocess", dependencies=[Depends(require_auth)])
async def reprocess(call_id: str):
    store = await Store().connect()
    try:
        call = await store.get_call(call_id)
        if not call:
            raise HTTPException(status_code=404, detail="no such call")
        audio_dir = Path(call.get("audio_dir") or f"data/recordings/{call_id}")
        kw = {}
        for role, name in (("caller_wav", "caller.wav"), ("bot_wav", "bot.wav"),
                           ("owner_wav", "owner.wav")):
            p = audio_dir / name
            if p.exists():
                kw[role] = p
        if not kw:
            # Audio is discarded after a successful summary (no recordings kept):
            # re-summarise from the saved transcript instead.
            if not await store.get_segments(call_id):
                raise HTTPException(status_code=422, detail="no transcript or audio to reprocess")
            await store.update_call(call_id, {"status": "processing"})
            try:
                await pipeline_mod.summarize_saved(
                    store, call_id, caller_label=call.get("caller", "Unknown"),
                    mode=call.get("mode", "notes"), started_at=str(call.get("started_at")),
                    ended_at=str(call.get("ended_at")) if call.get("ended_at") else None)
            except Exception as e:  # noqa: BLE001
                await store.update_call(call_id, {"status": "failed", "error": repr(e)[:500]})
                raise HTTPException(status_code=502, detail=f"summary failed: {e}") from e
            return _short(await store.get_call(call_id))
        await pipeline_mod.run_pipeline(
            call_id, caller_label=call.get("caller", "Unknown"),
            mode=call.get("mode", "notes"), started_at=str(call.get("started_at")),
            audio_dir=audio_dir, store=store,
            ended_at=str(call.get("ended_at")) if call.get("ended_at") else None, **kw)
        return _short(await store.get_call(call_id))
    finally:
        await store.close()


@router.post("/ask", dependencies=[Depends(require_auth)])
async def ask(body: dict):
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question required")
    store = await Store().connect()
    try:
        segs = await store.query(
            "SELECT id, call, t_start, speaker, text, search::score(1) AS score"
            " FROM segment WHERE text @1@ $q ORDER BY score DESC LIMIT 25", {"q": question}) or []
        calls = await store.query(
            "SELECT id, caller, started_at, title, summary, message_for_owner,"
            " search::score(1) AS score FROM call WHERE summary @1@ $q"
            " ORDER BY score DESC LIMIT 8", {"q": question}) or []
        evidence, seen = [], set()

        def cid_of(row) -> str:
            return str(row.get("call", row.get("id", ""))).removeprefix("call:")

        for r in segs:
            cid = cid_of(r)
            seen.add(cid)
            evidence.append({"kind": "segment", "call_id": cid, "caller": "?",
                             "date": "", "t_start": r.get("t_start", 0),
                             "speaker": r.get("speaker", ""), "text": r.get("text", "")})
        for cid in list(seen):
            c = await store.get_call(cid)
            if c:
                for e in evidence:
                    if e["call_id"] == cid:
                        e["caller"] = c.get("caller", "?")
                        e["date"] = str(c.get("started_at", ""))[:10]
        for r in calls:
            cid = str(r.get("id", "")).removeprefix("call:")
            seen.add(cid)
            evidence.append({"kind": "summary", "call_id": cid, "caller": r.get("caller", "?"),
                             "date": str(r.get("started_at", ""))[:10],
                             "title": r.get("title") or "", "summary": r.get("summary") or ""})
        if not evidence:
            recent = await store.query(
                "SELECT id, caller, started_at, title, summary FROM call"
                " ORDER BY started_at DESC LIMIT 10") or []
            for r in recent:
                evidence.append({"kind": "summary",
                                 "call_id": str(r.get("id", "")).removeprefix("call:"),
                                 "caller": r.get("caller", "?"),
                                 "date": str(r.get("started_at", ""))[:10],
                                 "title": r.get("title") or "", "summary": r.get("summary") or ""})
        # §8.4: Ask also covers chat messages + brains; citations say "chat".
        try:
            chat_hits = await store.query(
                "SELECT chat, sender, ts, text, search::score(1) AS score"
                " FROM message WHERE text @1@ $q ORDER BY score DESC LIMIT 15",
                {"q": question}) or []
            chat_names: dict[str, str] = {}
            for r in chat_hits:
                cid = str(r.get("chat", "")).removeprefix("chat:")
                if cid not in chat_names:
                    c = await store.get_chat(cid)
                    chat_names[cid] = str((c or {}).get("name", cid))
                evidence.append({"kind": "chat", "chat": cid,
                                 "name": chat_names[cid], "sender": r.get("sender", ""),
                                 "date": str(r.get("ts", ""))[:10],
                                 "text": r.get("text", "")})
            brains = await store.query("SELECT chat, summary FROM brain LIMIT 50") or []
            ql = question.lower()
            for b in brains:
                summary = str(b.get("summary") or "")
                words = [w for w in ql.split() if len(w) > 3]
                if summary and any(w in summary.lower() for w in words):
                    cid = str(b.get("chat", "")).removeprefix("chat:")
                    evidence.append({"kind": "brain", "chat": cid,
                                     "name": chat_names.get(cid, cid),
                                     "date": "", "text": summary[:500]})
        except Exception as e:  # noqa: BLE001 — chats are optional evidence
            log.warning("chat evidence skipped: %r", e)
        answer = await answer_question(question, evidence)
        citations = [{"call_id": e["call_id"], "t": e.get("t_start")} for e in evidence
                     if e.get("kind") in ("segment", "summary")]
        citations += [{"chat": e["chat"], "sender": e.get("sender"), "date": e.get("date")}
                      for e in evidence if e.get("kind") in ("chat", "brain")]
        # §8.4: evidence search also covers message + brain tables.
        return {"answer": answer, "citations": citations}
    finally:
        await store.close()


@router.get("/contacts", dependencies=[Depends(require_auth)])
async def get_contacts():
    return contacts_mod.load()


@router.put("/contacts", dependencies=[Depends(require_auth)])
async def put_contacts(body: dict):
    allowed = {"policy", "block", "allow", "notes"}
    if any(k not in allowed for k in body):
        raise HTTPException(status_code=422, detail="bad keys")
    if "policy" in body and body["policy"] not in ("all", "allowlist"):
        raise HTTPException(status_code=422, detail="policy must be all|allowlist")
    path = Path("contacts.yaml")
    if path.exists():
        shutil.copy(path, str(path) + ".bak")
    data = contacts_mod.load()
    data.update({k: body[k] for k in allowed if k in body})
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return data


@router.get("/brief", dependencies=[Depends(require_auth)])
async def get_brief():
    p = Path("brief.md")
    return {"brief": p.read_text() if p.exists() else ""}


@router.put("/brief", dependencies=[Depends(require_auth)])
async def put_brief(body: dict):
    Path("brief.md").write_text((body.get("brief") or "").rstrip("\n") + "\n")
    return await get_brief()


@router.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    out = runtime_settings.all_effective()
    out["groq_key"] = "set" if settings.GROQ_API_KEY else "missing"
    return out


@router.patch("/settings", dependencies=[Depends(require_auth)])
async def patch_settings(body: dict):
    body = dict(body)
    new_key = body.pop("groq_key", None)
    if body:
        try:
            runtime_settings.set_many(body)
        except KeyError as e:
            raise HTTPException(status_code=422, detail=f"bad setting: {e}")
    if new_key:
        _write_env_key("GROQ_API_KEY", str(new_key))
        settings.GROQ_API_KEY = str(new_key)
        from phathom.ai import groq_client
        groq_client._client = None
    return await get_settings()


def _write_env_key(key: str, value: str) -> None:
    """Update or append one KEY=value line in .env (Groq key setter only)."""
    p = Path(".env")
    lines = p.read_text().splitlines() if p.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            updated = True
    if not updated:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines).rstrip("\n") + "\n")


@router.get("/stats", dependencies=[Depends(require_auth)])
async def get_stats():
    store = await Store().connect()
    try:
        week = await store.query(
            "SELECT id, duration_s, action_items FROM call"
            " WHERE started_at > time::now() - 7d") or []
        minutes = sum(int(r.get("duration_s") or 0) for r in week) // 60
        open_items = sum(len(r.get("action_items") or []) for r in week)
        done = _load_done()
        open_items -= sum(1 for v in done.values() if v)
        return {"calls_this_week": len(week), "minutes_recorded": minutes,
                "open_action_items": max(0, open_items)}
    finally:
        await store.close()


def _load_done() -> dict:
    try:
        return json.loads(DONE_PATH.read_text())
    except (OSError, ValueError):
        return {}


@router.get("/action-items", dependencies=[Depends(require_auth)])
async def action_items(open: bool = True):
    store = await Store().connect()
    try:
        rows = await store.query(
            "SELECT id, caller, started_at, action_items FROM call"
            " ORDER BY started_at DESC LIMIT 200") or []
        done = _load_done()
        items = []
        for r in rows:
            cid = str(r.get("id", "")).removeprefix("call:")
            for i, a in enumerate(r.get("action_items") or []):
                iid = f"{cid}:{i}"
                is_done = bool(done.get(iid, False))
                if open and is_done:
                    continue
                items.append({"id": iid, "call_id": cid, "caller": r.get("caller"),
                              "date": str(r.get("started_at", ""))[:10],
                              "owner": a.get("owner"), "item": a.get("item"),
                              "due": a.get("due"), "done": is_done})
        return {"items": items}
    finally:
        await store.close()


@router.patch("/action-items/{item_id}", dependencies=[Depends(require_auth)])
async def patch_action_item(item_id: str, body: dict):
    done = _load_done()
    done[item_id] = bool(body.get("done", True))
    DONE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DONE_PATH.write_text(json.dumps(done, indent=2))
    return {"id": item_id, "done": done[item_id]}


def _tab_status(manager) -> dict:
    import time
    seen = float(getattr(manager, "tab_seen_at", 0.0) or 0.0)
    ago = round(time.time() - seen) if seen else None
    return {"reporting": ago is not None and ago < 30, "last_seen_s": ago}


def _live_selectors(manager) -> list[dict]:
    import time
    rows = getattr(manager, "selector_check", None)
    fresh = rows is not None and time.time() - float(getattr(manager, "selector_at", 0.0)) < 30
    names = ["LOGGED_IN_MARKER", "INCOMING_CALL_ROOT", "INCOMING_CALLER", "ACCEPT_BUTTON",
             "HANGUP_BUTTON", "CALL_TIMER"]
    if not fresh:
        return [{"name": n, "status": "unknown", "verified": True} for n in names]
    return [{"name": str(r.get("name")), "status": str(r.get("status")),
             "verified": bool(r.get("verified"))} for r in rows if isinstance(r, dict)]


@router.get("/diagnostics", dependencies=[Depends(require_auth)])
async def diagnostics(request: Request):
    """Diagnostics page data (EXTENSION_SPEC §7.5): hooks, WhatsApp status,
    caller source + per-tap meters, bot-voice path, selector check."""
    manager = getattr(request.app.state, "manager", None)
    st = state_mod.read()
    eff = runtime_settings.all_effective()
    hooks = dict(getattr(manager, "ext_hooks", {}) or {})
    for k in ("gum", "pc"):
        hooks.setdefault(k, False)
    taps: dict[str, float] = dict(getattr(manager, "tap_rms", {}) or {})
    session = getattr(manager, "session", None)
    active = None
    if session is not None:
        active = session.active_source
        for label, db in (session.tap_db or {}).items():
            taps.setdefault(label, db)
    heard = bool(eff.get("test_tone_heard", False))
    talk_on = bool(eff.get("talk_mode_enabled", True))
    return {
        "hooks": hooks,
        "worklet": getattr(manager, "worklet_mode", "unknown"),
        "wa": {"logged_in": st.get("wa_logged_in")},
        "caller_source": {
            "mode": eff.get("caller_source", "auto"),
            "active": active,
            "taps": [{"label": k, "db": v} for k, v in sorted(taps.items())],
        },
        "bot_voice": {
            "enabled": talk_on,
            "test_tone_heard": heard,
            "path": "verified" if heard else "unverified",
        },
        # Live: what the WhatsApp tab itself reports (every 10 s). Never guessed.
        "selectors": _live_selectors(manager),
        "tab": _tab_status(manager),
        "server": {
            "version": "0.2.0",
            "groq_key": "set" if settings.GROQ_API_KEY else "missing",
            "connected": bool(st.get("connected", False)),
            "in_call": bool(session.open) if session else False,
            "last_tone": getattr(manager, "last_tone", None),
        },
    }


@router.post("/dev/dump", dependencies=[Depends(require_auth)])
async def dev_dump(body: dict):
    """DOM dumps from the extension (EXTENSION_SPEC §11.4)."""
    from datetime import timezone as _tz
    html = body.get("html", "")
    label = "".join(c.lower() if c.isalnum() else "_" for c in body.get("label", "manual"))[:32] or "manual"
    ts = datetime.now(_tz.utc).strftime("%Y%m%d_%H%M%S")
    path = Path("data/dumps") / f"{ts}_{label}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
    return {"ok": True, "path": str(path), "chars": len(html)}


# ---- Brain routes (§8.5) ----

def _brain_short(chat: dict, brain: dict | None) -> dict:
    cid = str(chat.get("id", "")).removeprefix("chat:")
    return {"id": cid, "name": chat.get("name"), "kind": chat.get("kind", "direct"),
            "updated_at": str(brain.get("updated_at")) if brain and brain.get("updated_at") else None,
            "msg_count": chat.get("msg_count", 0)}


@router.get("/brain", dependencies=[Depends(require_auth)])
async def brain_list():
    from phathom import brain as brain_mod  # noqa: F401 (keeps import local to v2 routes)
    store = await Store().connect()
    try:
        chats = await store.list_chats()
        out = []
        for c in chats:
            cid = str(c.get("id", "")).removeprefix("chat:")
            out.append(_brain_short(c, await store.get_brain(cid)))
        return {"chats": out}
    finally:
        await store.close()


@router.get("/brain/{chat_id}", dependencies=[Depends(require_auth)])
async def brain_get(chat_id: str):
    store = await Store().connect()
    try:
        chat = await store.get_chat(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="no such chat")
        brain = await store.get_brain(chat_id) or {}
        detail = _brain_short(chat, brain)
        detail.update({"summary": brain.get("summary"), "people": brain.get("people", []),
                       "facts": brain.get("facts", []), "open_loops": brain.get("open_loops", []),
                       "decisions": brain.get("decisions", []), "timeline": brain.get("timeline", [])})
        return detail
    finally:
        await store.close()


async def _ingest_and_rebuild(store, chat_id: str, messages: list[dict]):
    from phathom import brain as brain_mod
    msgs = brain_mod.with_hashes(messages)
    added = await store.add_messages(chat_id, msgs)
    brain = await brain_mod.rebuild_brain(store, chat_id)
    return added, brain


@router.post("/brain/import", dependencies=[Depends(require_auth)])
async def brain_import(file: UploadFile = File(...), name: str | None = None,
                       kind: str = "direct"):
    """Import an exported chat (.txt/.zip from WhatsApp → Export chat)."""
    from phathom import brain as brain_mod
    raw = await file.read()
    suffix = Path(file.filename or "chat.txt").suffix.lower()
    tmp = Path(f"data/debug/brain_upload{suffix}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)
    try:
        text = brain_mod.read_export_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    chat_name = name or Path(file.filename or "chat").stem.replace("_", " ").strip() or "Imported chat"
    messages = brain_mod.parse_export(text)
    if not messages:
        raise HTTPException(status_code=422, detail="no messages parsed from export")
    store = await Store().connect()
    try:
        chat_id = brain_mod.chat_slug(chat_name)
        await store.upsert_chat(chat_id, chat_name, kind)
        added, _ = await _ingest_and_rebuild(store, chat_id, messages)
        return {"id": chat_id, "name": chat_name, "added": added,
                "total": len(messages)}
    finally:
        await store.close()


@router.post("/brain/{chat_id}/messages", dependencies=[Depends(require_auth)])
async def brain_messages(chat_id: str, body: dict):
    """Batch from the Web reader (content.js read_chat)."""
    from phathom import brain as brain_mod
    items = body.get("messages", [])
    messages = [{"ts": m.get("ts") or datetime.now(timezone.utc).isoformat(),
                 "sender": m.get("sender", ""), "is_me": bool(m.get("is_me")),
                 "text": m.get("text", "")} for m in items if (m.get("text") or "").strip()]
    if not messages:
        raise HTTPException(status_code=422, detail="no messages")
    store = await Store().connect()
    try:
        if not await store.get_chat(chat_id):
            await store.upsert_chat(chat_id, body.get("name", chat_id))
        added, _ = await _ingest_and_rebuild(store, chat_id, messages)
        return {"ok": True, "added": added}
    finally:
        await store.close()


@router.post("/brain/{chat_id}/rebuild", dependencies=[Depends(require_auth)])
async def brain_rebuild(chat_id: str):
    from phathom import brain as brain_mod
    store = await Store().connect()
    try:
        if not await store.get_chat(chat_id):
            raise HTTPException(status_code=404, detail="no such chat")
        await brain_mod.rebuild_brain(store, chat_id)
        return {"ok": True}
    finally:
        await store.close()


@router.delete("/brain/{chat_id}", dependencies=[Depends(require_auth)])
async def brain_delete(chat_id: str):
    store = await Store().connect()
    try:
        if not await store.get_chat(chat_id):
            raise HTTPException(status_code=404, detail="no such chat")
        await store.delete_brain(chat_id)
        return {"ok": True}
    finally:
        await store.close()
