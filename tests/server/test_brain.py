"""Brain tests (EXTENSION_SPEC step B6): export parsers (Android + iOS fixtures),
dedup hashes, local merge, and a live-DB chat/message/brain roundtrip."""

import pytest

from phathom import brain as brain_mod
from phathom.store import Store, close_shared

ANDROID = """12/05/2026, 21:34 - Ahmed: hello, is the report ready?
12/05/2026, 21:35 - Daniyal: almost, sending tonight
it just needs the final chart
12/05/2026, 21:36 - Ahmed: great, thanks!
Messages and calls are end-to-end encrypted. No one outside of this chat can read them.
"""

IOS = """[12/05/2026, 09:34:05 PM] Ammi: beta, did you eat?
[12/05/2026, 09:35:10 PM] Daniyal: yes Ammi, just now
[12/05/2026, 09:36:00 PM] Ammi: good. Call on Sunday
and bring the books"""

ANDROID_24H = "05/12/2026, 08:05 - Ahmed: morning\n"


def test_android_format_and_multiline():
    msgs = brain_mod.parse_export(ANDROID, me_names=("Daniyal",))
    assert len(msgs) == 3
    assert msgs[0]["sender"] == "Ahmed" and msgs[0]["ts"].startswith("2026-05-12T21:34")
    assert msgs[1]["is_me"] is True
    assert "final chart" in msgs[1]["text"]  # continuation line joined
    assert all("end-to-end encrypted" not in m["text"] for m in msgs)


def test_ios_format_12h_and_multiline():
    msgs = brain_mod.parse_export(IOS, me_names=("Daniyal",))
    assert len(msgs) == 3
    assert msgs[0]["sender"] == "Ammi" and msgs[0]["ts"].startswith("2026-05-12T21:34")
    assert msgs[1]["is_me"] is True
    assert "bring the books" in msgs[2]["text"]


def test_day_month_order_not_swapped():
    msgs = brain_mod.parse_export(ANDROID_24H)
    assert msgs[0]["ts"].startswith("2026-12-05T08:05")


def test_hashes_stable_and_distinct():
    a = brain_mod.msg_hash("t", "s", "x")
    assert a == brain_mod.msg_hash("t", "s", "x")
    assert a != brain_mod.msg_hash("t", "s", "y")


def test_merge_local_dedupes_and_caps():
    old = {"facts": [{"text": "likes chai", "message_ts": "2026-01-01"}],
           "people": [{"name": "Ahmed", "note": "classmate"}]}
    new = {"facts": [{"text": "likes chai", "message_ts": "2026-02-01"},
                     {"text": "deadline Friday", "message_ts": "2026-02-01"}],
           "people": [{"name": "Ahmed", "note": "FYP partner"}]}
    merged = brain_mod.merge_local(old, new)
    texts = [f["text"] for f in merged["facts"]]
    assert texts == ["likes chai", "deadline Friday"]
    assert merged["people"] == [{"name": "Ahmed", "note": "FYP partner"}]  # newest wins


async def test_rebuild_with_mock_llm(monkeypatch):
    """rebuild_brain with a stubbed extractor: windows → merge → saved."""
    async def fake_extract(window: str):
        assert "Ahmed" in window
        return {"facts": [{"text": "report due", "message_ts": "2026-05-12"}],
                "open_loops": [], "decisions": [],
                "people": [{"name": "Ahmed", "note": ""}], "timeline_events": []}

    async def fake_merge(old: dict, new: dict):
        out = brain_mod.merge_local(old, new)
        out["summary"] = "Ahmed waits for the report."
        return out

    monkeypatch.setattr(brain_mod, "_extract_window", fake_extract)
    monkeypatch.setattr(brain_mod, "_merge", fake_merge)

    class MemStore:
        def __init__(self):
            self.chats, self.msgs, self.brains = {}, [], {}

        async def get_chat(self, cid):
            return self.chats.get(cid)

        async def get_brain(self, cid):
            return self.brains.get(cid)

        async def get_messages_since(self, cid, since):
            return self.msgs

        async def save_brain(self, cid, brain):
            self.brains[cid] = brain

    store = MemStore()
    store.chats["ahmed"] = {"id": "chat:ahmed", "name": "Ahmed"}
    store.msgs = [{"ts": "2026-05-12T21:34:00+00:00", "sender": "Ahmed",
                   "is_me": False, "text": "is the report ready?", "hash": "h1"}]
    brain = await brain_mod.rebuild_brain(store, "ahmed")
    assert brain["summary"] == "Ahmed waits for the report."
    assert brain["last_msg_ts"].startswith("2026-05-12")


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


async def test_chat_message_brain_roundtrip(store: Store):
    cid = "trip Ahmed".lower().replace(" ", "_")
    await store.upsert_chat(cid, "Trip Ahmed")
    msgs = brain_mod.with_hashes([
        {"ts": "2026-05-12T21:34:00+00:00", "sender": "Trip Ahmed",
         "is_me": False, "text": "hello"},
        {"ts": "2026-05-12T21:35:00+00:00", "sender": "Me",
         "is_me": True, "text": "hi"},
    ])
    assert await store.add_messages(cid, msgs) == 2
    assert await store.add_messages(cid, msgs) == 0  # re-import never duplicates
    got = await store.get_messages_since(cid, None)
    assert len(got) == 2
    await store.save_brain(cid, {"summary": "s", "people": [], "facts": [],
                                 "open_loops": [], "decisions": [], "timeline": [],
                                 "last_msg_ts": "2026-05-12T21:35:00+00:00"})
    brains = await store.get_brain(cid)
    assert brains and brains["summary"] == "s"
    chats = await store.list_chats()
    assert any(c["name"] == "Trip Ahmed" for c in chats)
    assert await brain_mod.brain_context_for(store, "Trip Ahmed") == "s"
    await store.delete_brain(cid)
    assert await store.get_chat(cid) is None


async def test_brain_api_import_get_delete(monkeypatch):
    """Brain REST routes with an in-memory store and stubbed LLM rebuild."""
    import io

    import phathom.server.api as api_mod
    from fastapi.testclient import TestClient

    from phathom.server import auth
    from phathom.server.app import create_app
    from phathom.server.ws import ExtensionManager

    class MemStore:
        def __init__(self):
            self.chats, self.msgs, self.brains = {}, [], {}

        async def connect(self):
            return self

        async def close(self):
            pass

        async def query(self, *a, **k):
            return []

        async def upsert_chat(self, cid, name, kind="direct"):
            self.chats[cid] = {"id": f"chat:{cid}", "name": name, "kind": kind,
                               "msg_count": 0}

        async def list_chats(self):
            return list(self.chats.values())

        async def get_chat(self, cid):
            return self.chats.get(cid)

        async def add_messages(self, cid, messages):
            self.msgs.extend(messages)
            return len(messages)

        async def get_messages_since(self, cid, since, limit=5000):
            return self.msgs

        async def save_brain(self, cid, brain):
            self.brains[cid] = brain

        async def get_brain(self, cid):
            return self.brains.get(cid)

        async def delete_brain(self, cid):
            self.chats.pop(cid, None)
            self.brains.pop(cid, None)

    mem = MemStore()
    monkeypatch.setattr(api_mod, "Store", lambda *a, **k: mem)

    async def fake_rebuild(store, chat_id, progress=None):
        brain = {"summary": "stub brain", "people": [], "facts": [],
                 "open_loops": [], "decisions": [], "timeline": [],
                 "last_msg_ts": "2026-05-12T21:35:00+00:00"}
        await store.save_brain(chat_id, brain)
        return brain

    monkeypatch.setattr(brain_mod, "rebuild_brain", fake_rebuild)

    app = create_app(ExtensionManager())
    c = TestClient(app, raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {auth.get_or_create_token()}"}
    txt = ("12/05/2026, 21:34 - Ahmed: hello\n"
           "12/05/2026, 21:35 - Ahmed: is the report ready?\n")
    r = c.post("/api/brain/import", files={"file": ("chat.txt", io.BytesIO(txt.encode()))},
               headers=h)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["added"] == 2
    r = c.get("/api/brain", headers=h)
    assert any(x["id"] == cid for x in r.json()["chats"])
    r = c.get(f"/api/brain/{cid}", headers=h)
    assert r.json()["summary"] == "stub brain"
    assert c.delete(f"/api/brain/{cid}", headers=h).json() == {"ok": True}
    assert c.get(f"/api/brain/{cid}", headers=h).status_code == 404
