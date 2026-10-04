"""REST API tests with a fake store (no SurrealDB needed)."""

import json

import pytest
from fastapi.testclient import TestClient

import phathom.server.api as api_mod
from phathom.server import auth
from phathom.server.app import create_app
from phathom.server.ws import ExtensionManager


class FakeStore:
    def __init__(self, *a, **k):
        self.calls: dict = {}
        self.segments: dict = {}
        self.queries: list = []

    async def connect(self):
        return self

    async def close(self):
        pass

    async def query(self, sql, vars=None):
        self.queries.append(sql)
        return []

    async def create_call(self, call_id, caller, started_at, mode, status="live", audio_dir=None):
        self.calls[call_id] = {"id": f"call:{call_id}", "caller": caller,
                               "started_at": started_at, "mode": mode, "status": status,
                               "audio_dir": audio_dir, "title": None,
                               "message_for_owner": None, "starred": False}
        return self.calls[call_id]

    async def update_call(self, call_id, fields):
        self.calls[call_id].update(fields)
        return self.calls[call_id]

    async def get_call(self, call_id):
        return self.calls.get(call_id)

    async def list_calls(self, limit=20):
        return list(self.calls.values())[:limit]

    async def add_segments(self, call_id, segments):
        self.segments.setdefault(call_id, []).extend(segments)

    async def delete_segments(self, call_id, source=None):
        self.segments[call_id] = []

    async def get_segments(self, call_id):
        return self.segments.get(call_id, [])


@pytest.fixture()
def client(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(api_mod, "Store", lambda *a, **k: store)
    app = create_app(ExtensionManager())
    c = TestClient(app, raise_server_exceptions=False)
    c.store = store
    c.token = auth.get_or_create_token()
    c.h = {"Authorization": f"Bearer {c.token}"}
    return c


def test_auth_required(client):
    r = client.get("/api/health")
    assert r.status_code == 401
    r = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_health_and_origin(client):
    r = client.get("/api/health", headers=client.h)
    assert r.status_code == 200 and r.json()["server"] == "phathom"
    r = client.get("/api/health", headers={**client.h, "Origin": "https://evil.com"})
    assert r.status_code == 403


def test_state_roundtrip(client):
    r = client.get("/api/state", headers=client.h)
    assert r.status_code == 200
    r = client.patch("/api/state", json={"connected": True, "mode": "talk", "copilot": False},
                     headers=client.h)
    body = r.json()
    assert body["connected"] is True and body["mode"] == "assistant"  # talk -> assistant
    assert body["answer"] == "talk" and body["copilot"] is False
    r = client.patch("/api/state", json={"mode": "nap"}, headers=client.h)
    assert r.status_code == 422


def test_calls_crud_and_filters(client):
    assert client.get("/api/calls", headers=client.h).json() == {"calls": [], "next_cursor": None}
    assert client.get("/api/calls/nope", headers=client.h).status_code == 404
    assert client.patch("/api/calls/nope", json={"title": "x"}, headers=client.h).status_code == 404
    assert client.delete("/api/calls/nope", headers=client.h).status_code == 404
    assert client.post("/api/calls/nope/reprocess", headers=client.h).status_code == 404
    assert client.get("/api/calls/nope/audio/caller", headers=client.h).status_code == 404


def test_contacts_and_brief(client):
    r = client.get("/api/contacts", headers=client.h)
    assert "policy" in r.json()
    r = client.put("/api/contacts", json={"policy": "sometimes"}, headers=client.h)
    assert r.status_code == 422
    r = client.put("/api/contacts", json={"policy": "all", "block": ["Unknown"]}, headers=client.h)
    assert r.json()["block"] == ["Unknown"]
    r = client.put("/api/brief", json={"brief": "hello"}, headers=client.h)
    assert r.json() == {"brief": "hello\n"}
    assert client.get("/api/brief", headers=client.h).json() == {"brief": "hello\n"}


def test_settings_never_leaks_key(client, monkeypatch):
    monkeypatch.setattr("phathom.server.api.settings",
                        type("S", (), {"GROQ_API_KEY": "gsk_super_secret_123"})())
    r = client.get("/api/settings", headers=client.h)
    body = r.json()
    assert body["groq_key"] == "set"
    assert "gsk_super_secret_123" not in json.dumps(body)


def test_ask_and_action_items(client, monkeypatch):
    async def fake_answer(question, evidence):
        return "no idea"
    monkeypatch.setattr(api_mod, "answer_question", fake_answer)
    r = client.post("/api/ask", json={"question": "what?"}, headers=client.h)
    assert r.json() == {"answer": "no idea", "citations": []}
    assert client.post("/api/ask", json={"question": ""}, headers=client.h).status_code == 422
    assert client.get("/api/action-items", headers=client.h).json() == {"items": []}
    r = client.patch("/api/action-items/abc:0", json={"done": True}, headers=client.h)
    assert r.json() == {"id": "abc:0", "done": True}


def test_stats_and_dump(client):
    assert client.get("/api/stats", headers=client.h).json() == {
        "calls_this_week": 0, "minutes_recorded": 0, "open_action_items": 0}
    r = client.post("/api/dev/dump", json={"label": "t", "html": "<html></html>"},
                    headers=client.h)
    assert r.json()["ok"] is True and r.json()["chars"] == len("<html></html>")
