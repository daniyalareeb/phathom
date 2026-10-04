"""No recordings kept: audio is deleted after a successful summary; a call can
still be re-summarised from its saved transcript."""

from types import SimpleNamespace

import phathom.pipeline as pipeline
from phathom.store import Store, close_shared


def _fake_summary():
    return SimpleNamespace(language="en", title="Invoice", summary="Sara needs the invoice.",
                           key_points=["invoice"], action_items=[], message_for_owner=None,
                           follow_up_needed=False)


async def test_discard_audio_respects_setting(tmp_path, monkeypatch):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "caller.wav").write_bytes(b"x")
    monkeypatch.setattr(pipeline.settings, "KEEP_RECORDINGS", True)
    pipeline.discard_audio(d)
    assert d.exists()
    monkeypatch.setattr(pipeline.settings, "KEEP_RECORDINGS", False)
    pipeline.discard_audio(d)
    assert not d.exists()


async def test_summarize_saved_from_transcript_only(monkeypatch):
    async def fake_summarize(meta, transcript):
        assert "invoice by Friday" in transcript
        return _fake_summary()

    async def fake_notify(*a, **k):
        pass

    monkeypatch.setattr(pipeline, "summarize", fake_summarize)
    monkeypatch.setattr(pipeline, "notify", fake_notify)
    s = await Store(url="mem://").connect()
    await s.create_call("c1", "Sara", "2026-10-04T10:00:00+00:00", "copilot")
    await s.add_segments("c1", [{"t_start": 0, "t_end": 3, "speaker": "caller",
                                 "text": "I need the invoice by Friday", "source": "final"}])
    await pipeline.summarize_saved(s, "c1", caller_label="Sara", mode="copilot",
                                   started_at="2026-10-04T10:00:00+00:00",
                                   ended_at="2026-10-04T10:01:00+00:00")
    c = await s.get_call("c1")
    assert c["status"] == "done" and c["summary"] == "Sara needs the invoice." and c["duration_s"] == 60
    await close_shared()
