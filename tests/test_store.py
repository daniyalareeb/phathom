"""Store tests — SPEC Phase 2 acceptance. Separate namespace, cleaned up afterwards."""

import pytest

from phathom.store import Store, close_shared, make_call_id, slug_caller


@pytest.fixture()
async def store():
    s = await Store(url="mem://", ns="phathom_test", db="main").connect()
    yield s
    try:
        await s.query("REMOVE NAMESPACE phathom_test")
    except Exception:
        pass
    await s.close()
    await close_shared()


def test_slug():
    assert slug_caller("Ahmed Uni") == "ahmed_uni"
    assert slug_caller("~Ftm studios") == "ftm_studios"
    assert slug_caller("") == "unknown"
    assert " " not in make_call_id("Ammi")


async def test_create_get_update(store: Store):
    cid = make_call_id("Ahmed Uni")
    assert await store.get_call(cid) is None
    c = await store.create_call(cid, "Ahmed Uni", "2026-10-02T10:00:00Z", "notes")
    assert c["caller"] == "Ahmed Uni" and c["status"] == "live"
    u = await store.update_call(cid, {"status": "done", "title": "Hi", "duration_s": 12})
    assert u["status"] == "done" and u["title"] == "Hi" and u["duration_s"] == 12
    assert (await store.get_call(cid))["title"] == "Hi"


async def test_segments(store: Store):
    cid = make_call_id("Ammi")
    await store.create_call(cid, "Ammi", "2026-10-02T10:00:00Z", "notes")
    await store.add_segments(cid, [
        {"t_start": 2.0, "t_end": 3.0, "speaker": "caller", "text": "b", "source": "live"},
        {"t_start": 0.0, "t_end": 1.0, "speaker": "caller", "text": "a", "source": "live"},
    ])
    segs = await store.get_segments(cid)
    assert [s["text"] for s in segs] == ["a", "b"]  # ordered by t_start
    await store.delete_segments(cid, source="live")
    assert await store.get_segments(cid) == []
    rows = await store.list_calls(10)
    assert {r["caller"] for r in rows} >= {"Ammi"}


async def test_brain_arrays_keep_their_items(store: Store):
    """Facts / open loops / people must survive a save (they used to be dropped)."""
    await store.upsert_chat("sara", "Sara")
    await store.save_brain("sara", {
        "summary": "s", "people": [{"name": "Sara"}],
        "facts": [{"text": "invoices monthly", "message_ts": "2026-10-01"}],
        "open_loops": ["invoice due Friday"], "decisions": [], "timeline": []})
    b = await store.get_brain("sara")
    assert b["facts"] == [{"text": "invoices monthly", "message_ts": "2026-10-01"}]
    assert b["open_loops"] == ["invoice due Friday"]
    assert b["people"] == [{"name": "Sara"}]


async def test_embedded_concurrent_first_use_and_writes(tmp_path):
    """Many requests opening the embedded DB at once (dashboard first load) must
    bootstrap once and not lose or conflict writes; data survives a reopen."""
    import asyncio
    url = f"surrealkv://{tmp_path}/db"

    async def one(i: int):
        s = await Store(url=url).connect()
        if i % 3 == 0:
            await s.create_call(f"c{i}", "X", "2026-10-03T00:00:00Z", "notes")
            await s.add_segments(f"c{i}", [{"t_start": 0, "t_end": 1, "speaker": "caller",
                                            "text": "report", "source": "final"}])
        await s.list_calls(50)
        await s.close()

    await asyncio.gather(*[one(i) for i in range(30)])
    await close_shared()
    s = await Store(url=url).connect()
    assert len(await s.list_calls(50)) == 10
    await close_shared()
