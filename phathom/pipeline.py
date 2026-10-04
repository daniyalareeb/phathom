"""After-call processing — SPEC §9.10. Runs as a background task; never loses audio."""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from phathom.ai.llm import summarize
from phathom.ai.prompts import fixed_line
from phathom.ai.stt import transcribe_file
from phathom.audio.devices import _run as run_cmd
from phathom.config import settings
from phathom.store import Store

log = logging.getLogger("phathom.pipeline")


def mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:02d}:{t % 60:02d}"


def speaker_label(speaker: str) -> str:
    return {"caller": "Caller", "owner": settings.OWNER_SHORT_NAME}.get(speaker, "Phathom")


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for s in sorted(segments, key=lambda x: x["t_start"]):
        who = speaker_label(s["speaker"])
        lines.append(f"[{mmss(s['t_start'])}] {who}: {s['text']}")
    return "\n".join(lines)


async def build_mixed(audio_dir: Path, caller_wav: Path | None, bot_wav: Path | None) -> Path | None:
    mixed = audio_dir / "mixed.mp3"
    if caller_wav and caller_wav.exists() and bot_wav and bot_wav.exists():
        rc, _, err = await run_cmd(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(caller_wav), "-i", str(bot_wav),
            "-filter_complex", "amix=inputs=2", "-ac", "1", "-b:a", "48k", str(mixed))
    elif caller_wav and caller_wav.exists():
        rc, _, err = await run_cmd(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(caller_wav), "-ac", "1", "-b:a", "48k", str(mixed))
    else:
        return None
    if rc != 0 or not mixed.exists():
        log.warning("mixed.mp3 build failed: %s", err)
        return None
    return mixed


async def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        await run_cmd("notify-send", title, body)


async def retention_cleanup(recordings_root: Path = Path("data/recordings")) -> None:
    """Once a day: delete recording folders older than KEEP_AUDIO_DAYS (transcripts stay)."""
    marker = Path("data/.last_audio_cleanup")
    today = datetime.now(timezone.utc).date().isoformat()
    if marker.exists() and marker.read_text().strip() == today:
        return
    cutoff = datetime.now(timezone.utc).timestamp() - settings.KEEP_AUDIO_DAYS * 86400
    taps_cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400  # §5.7: raw taps 7 days
    if recordings_root.exists():
        for child in recordings_root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
                    log.info("retention: removed %s", child)
                elif child.is_dir():
                    taps = child / "taps"
                    if taps.is_dir() and taps.stat().st_mtime < taps_cutoff:
                        shutil.rmtree(taps)
                        log.info("retention: removed per-tap wavs %s", taps)
            except OSError as e:
                log.warning("retention: %s: %r", child, e)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(today)


async def _transcribe_track(wav, speaker: str) -> list[dict]:
    if not wav or not Path(wav).exists():
        return []
    raw = await transcribe_file(Path(wav), settings.STT_MODEL_FINAL)
    # stt returns {start,end,text}; store uses t_start/t_end
    return [{"t_start": s["start"], "t_end": s["end"], "speaker": speaker,
             "source": "final", "text": s["text"]} for s in raw]


def _ended(started_at: str, ended_at: str | None) -> tuple[datetime, int]:
    """Call end + duration. ended_at is when the call ended, NOT when processing finished."""
    ended = datetime.fromisoformat(ended_at) if ended_at else datetime.now(timezone.utc)
    return ended, max(0, int((ended - datetime.fromisoformat(started_at)).total_seconds()))


async def _transcribe_segments(caller_wav, bot_wav, mode, disclosure_played, owner_wav=None):
    """STT + bot/disclosure assembly shared by online and offline paths."""
    final_caller = await _transcribe_track(caller_wav, "caller")
    final_owner = await _transcribe_track(owner_wav, "owner")
    bot_final = []
    if mode == "notes" and bot_wav and Path(bot_wav).exists() and disclosure_played:
        bot_final = [{"t_start": 0.0, "t_end": 8.0, "speaker": "phathom",
                      "text": fixed_line("disclosure", "en"), "source": "final"}]
    return final_caller + final_owner + bot_final


def discard_audio(audio_dir: Path) -> None:
    """No recordings kept (KEEP_RECORDINGS=false): the audio was only needed to
    transcribe; the call keeps its transcript + summary."""
    if settings.KEEP_RECORDINGS:
        return
    try:
        shutil.rmtree(audio_dir, ignore_errors=True)
        log.info("audio discarded for %s (summary + transcript kept)", Path(audio_dir).name)
    except OSError as e:
        log.warning("could not discard audio %s: %r", audio_dir, e)


async def summarize_saved(store: Store, call_id: str, *, caller_label: str, mode: str,
                          started_at: str, ended_at: str | None = None) -> int:
    """Summarise the call from its saved transcript and store the result.
    Used after transcription and by Reprocess when the audio is gone."""
    segments = await store.get_segments(call_id)
    ended, duration_s = _ended(started_at, ended_at)
    if not [s for s in segments if s.get("speaker") != "phathom"]:
        await store.update_call(call_id, {
            "ended_at": ended.isoformat(), "duration_s": duration_s, "status": "done",
            "title": "No conversation", "error": None})
        log.info("pipeline: no speech in %s; skipped summary", call_id)
        return len(segments)
    summary = await summarize(
        {"caller": caller_label, "started_at": started_at,
         "duration": f"{duration_s // 60} min {duration_s % 60} s", "mode": mode},
        format_transcript(segments))
    await store.update_call(call_id, {
        "ended_at": ended.isoformat(), "duration_s": duration_s, "status": "done",
        "language": summary.language, "title": summary.title, "summary": summary.summary,
        "key_points": summary.key_points,
        "action_items": [a.model_dump() for a in summary.action_items],
        "message_for_owner": summary.message_for_owner,
        "follow_up_needed": summary.follow_up_needed, "error": None,
    })
    headline = summary.message_for_owner or (summary.key_points[0] if summary.key_points else summary.title)
    await notify(f"Phathom · {caller_label} ({duration_s // 60} min)", f"{summary.title} — {headline}")
    return len(segments)


async def run_pipeline(call_id: str, *, caller_label: str, mode: str, started_at: str,
                       audio_dir: Path, caller_wav: Path | None = None,
                       bot_wav: Path | None = None,
                       live_bot_segments: list[dict] | None = None,
                       disclosure_played: bool = False,
                       store: Store | None = None,
                       owner_wav: Path | None = None,
                       ended_at: str | None = None) -> None:
    """Full after-call pipeline for one call. Idempotent-ish: safe to re-run via process."""
    own_store = store is None
    store = store or await Store().connect()
    audio_dir = Path(audio_dir)
    try:
        await store.update_call(call_id, {"status": "processing"})
        await build_mixed(audio_dir, caller_wav, owner_wav or bot_wav)

        transcribed = await _transcribe_segments(caller_wav, bot_wav, mode, disclosure_played, owner_wav)
        people = [s for s in transcribed if s["speaker"] != "phathom"]
        # Bot segments: reuse the live phathom segments (we know exactly what was said).
        # In notes mode there are none live, so use the disclosure insert from transcription.
        bot_final = list(live_bot_segments or []) or [s for s in transcribed if s["speaker"] == "phathom"]
        # Final transcription replaces everything saved live during the call.
        await store.delete_segments(call_id)
        await store.add_segments(call_id, people + bot_final)

        n = await summarize_saved(store, call_id, caller_label=caller_label, mode=mode,
                                  started_at=started_at, ended_at=ended_at)
        discard_audio(audio_dir)
        await retention_cleanup()
        log.info("pipeline done for %s (%d segments)", call_id, n)
    except Exception as e:
        log.exception("pipeline failed for %s", call_id)
        try:
            await store.update_call(call_id, {"status": "failed", "error": repr(e)})
        except Exception:
            pass
        await notify("Phathom · processing failed", f"{caller_label}: {e!r}. Audio kept; re-run with phathom process.")
    finally:
        if own_store:
            await store.close()


async def run_offline(call_id: str, *, caller_label: str, mode: str, started_at: str,
                      audio_dir: Path, caller_wav: Path | None = None,
                      bot_wav: Path | None = None,
                      live_bot_segments: list[dict] | None = None,
                      disclosure_played: bool = False,
                      owner_wav: Path | None = None,
                      ended_at: str | None = None) -> dict:
    """Same work as run_pipeline but without a DB (SPEC §12: SurrealDB down).
    Returns a result dict; the daemon writes it to result.json."""
    audio_dir = Path(audio_dir)
    result: dict = {"call_id": call_id, "caller_label": caller_label, "mode": mode,
                    "started_at": started_at, "audio_dir": str(audio_dir),
                    "segments": [], "summary": None, "error": None}
    try:
        second = owner_wav or bot_wav
        await build_mixed(audio_dir, Path(caller_wav) if caller_wav else None,
                          Path(second) if second else None)
        transcribed = await _transcribe_segments(caller_wav, bot_wav, mode, disclosure_played, owner_wav)
        segments = ([s for s in transcribed if s["speaker"] != "phathom"]
                    + (list(live_bot_segments or [])
                       or [s for s in transcribed if s["speaker"] == "phathom"]))
        result["segments"] = segments
        ended, duration_s = _ended(started_at, ended_at)
        result.update(ended_at=ended.isoformat(), duration_s=duration_s)
        summary = await summarize(
            {"caller": caller_label, "started_at": started_at,
             "duration": f"{duration_s // 60} min {duration_s % 60} s", "mode": mode},
            format_transcript(segments) or "(no speech transcribed)")
        result["summary"] = summary.model_dump()
        mins = duration_s // 60
        headline = summary.message_for_owner or (summary.key_points[0] if summary.key_points else summary.title)
        await notify(f"Phathom · {caller_label} ({mins} min)", f"{summary.title} — {headline}")
        await retention_cleanup()
    except Exception as e:
        log.exception("offline pipeline failed for %s", call_id)
        result["error"] = repr(e)
    return result


async def import_result_json(path: Path, store: Store) -> str:
    """Import a result.json written while the DB was down. Returns the call id."""
    data = json.loads(Path(path).read_text())
    call_id = data["call_id"]
    await store.create_call(call_id, data.get("caller_label", "Unknown"),
                            data.get("started_at", datetime.now(timezone.utc).isoformat()),
                            data.get("mode", "notes"), status="processing",
                            audio_dir=data.get("audio_dir"))
    await store.add_segments(call_id, data.get("segments", []))
    summary = data.get("summary") or {}
    await store.update_call(call_id, {
        "ended_at": data.get("ended_at", datetime.now(timezone.utc).isoformat()),
        "duration_s": data.get("duration_s", 0),
        "status": "failed" if data.get("error") else "done",
        "language": summary.get("language"), "title": summary.get("title"),
        "summary": summary.get("summary"), "key_points": summary.get("key_points", []),
        "action_items": summary.get("action_items", []),
        "message_for_owner": summary.get("message_for_owner"),
        "follow_up_needed": summary.get("follow_up_needed", False),
        "error": data.get("error"),
    })
    return call_id
