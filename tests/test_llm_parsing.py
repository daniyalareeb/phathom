"""LLM parsing tests — SPEC Phase 2 acceptance (mocked Groq).
Covers: good JSON, bad JSON -> retry -> success, bad JSON twice -> failure."""

import json
from types import SimpleNamespace

import groq
import httpx
import pytest

import phathom.ai.llm as llm_mod
from phathom.ai.llm import summarize


GOOD = {
    "title": "Report deadline",
    "language": "English/Urdu",
    "summary": "Ahmed asked for the draft tonight. Slides due Monday.",
    "key_points": ["Deadline Friday 5pm"],
    "action_items": [{"owner": "Daniyal", "item": "Send draft", "due": None}],
    "message_for_owner": "Send Ahmed the draft tonight.",
    "follow_up_needed": True,
}


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)  # each item: str (content) or Exception
        self.models = []

    async def create(self, **kwargs):
        self.models.append(kwargs.get("model"))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))])


class FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


def rate_limit_error():
    return groq.RateLimitError(
        "429", response=httpx.Response(429, headers={"retry-after": "0"},
                                       request=httpx.Request("POST", "https://api.groq.com")),
        body={})


META = {"caller": "Ahmed Uni", "started_at": "2026-10-02T10:00:00Z",
        "duration": "1 min 0 s", "mode": "manual"}


async def test_summarize_good_json(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_client", lambda: FakeClient([json.dumps(GOOD)]))
    out = await summarize(META, "[00:00] Caller: send the draft")
    assert out.title == "Report deadline" and out.follow_up_needed is True
    assert out.action_items[0].owner == "Daniyal"


async def test_summarize_bad_then_good(monkeypatch):
    fake = FakeClient(["not json {{{", json.dumps(GOOD)])
    monkeypatch.setattr(llm_mod, "get_client", lambda: fake)
    out = await summarize(META, "[00:00] Caller: send the draft")
    assert out.title == "Report deadline"
    assert len(fake.chat.completions.models) == 2  # retried once


async def test_summarize_bad_twice_fails(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_client", lambda: FakeClient(["nope", "still nope"]))
    with pytest.raises(json.JSONDecodeError):
        await summarize(META, "[00:00] Caller: send the draft")


async def test_summarize_429_falls_back(monkeypatch):
    monkeypatch.setattr(llm_mod.settings, "LLM_MODEL_SMART", "smart-model")
    monkeypatch.setattr(llm_mod.settings, "LLM_MODEL_LIVE", "live-model")
    # 4x 429 exhausts the retry policy (retry-after: 0, so no real sleeping), then fallback
    fake = FakeClient([rate_limit_error()] * 4 + [json.dumps(GOOD)])
    monkeypatch.setattr(llm_mod, "get_client", lambda: fake)
    out = await summarize(META, "[00:00] Caller: send the draft")
    assert out.title == "Report deadline"
    assert fake.chat.completions.models[-1] == "live-model"  # fell back once
    assert fake.chat.completions.models[0] == "smart-model"
