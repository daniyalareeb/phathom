"""Caller-source auto-pick (EXTENSION_SPEC §5.7).

The caller tap is the one with the most VAD-voiced frames that are NOT
simultaneous with owner speech (this rejects mic echoes and silent
keep-alive nodes). Pure functions: fully unit-tested with synthetic streams.
"""

from __future__ import annotations

# One bucket = 100 ms of aligned VAD evidence:
# {"owner": int, "<tap label>": int} of voiced 30 ms frames in that bucket.
Bucket = dict[str, int]

OWNER_KEY = "owner"


def score_taps(buckets: list[Bucket]) -> dict[str, int]:
    """Exclusive-voice score per tap: voiced frames minus owner-simultaneous ones.

    A tap frame counts only when the owner was silent in the same bucket.
    """
    scores: dict[str, int] = {}
    for b in buckets:
        owner_voiced = int(b.get(OWNER_KEY, 0))
        for label, voiced in b.items():
            if label == OWNER_KEY:
                continue
            if owner_voiced > 0:
                continue  # same-bucket owner speech: likely echo, ignore
            scores[label] = scores.get(label, 0) + int(voiced)
    return scores


def pick_caller(buckets: list[Bucket]) -> str | None:
    """Return the winning tap label, or None when nothing is voiced."""
    scores = score_taps(buckets)
    if not scores:
        return None
    best = max(scores.items(), key=lambda kv: (kv[1], kv[0]))
    return best[0] if best[1] > 0 else None


def resolve_source(*, mode: str, webrtc_seen: bool, tab_seen: bool,
                   buckets: list[Bucket]) -> str | None:
    """`auto` order (§5.7): webrtc if a remote track appeared, else tab_capture
    if a capture is active, else the speaker_tap auto-pick. Explicit modes
    force one source (the session maps it to the caller track).
    Returns a source key: "webrtc" | "tab" | "speaker:<label>" | None.
    """
    if mode == "webrtc":
        return "webrtc" if webrtc_seen else None
    if mode == "tab_capture":
        return "tab" if tab_seen else None
    if mode == "speaker_tap":
        winner = pick_caller(buckets)
        return f"speaker:{winner}" if winner else None
    # auto
    if webrtc_seen:
        return "webrtc"
    if tab_seen:
        return "tab"
    winner = pick_caller(buckets)
    return f"speaker:{winner}" if winner else None
