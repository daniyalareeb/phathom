"""Ask must accept chat + brain evidence (§8.4), not only call evidence."""

from types import SimpleNamespace

from phathom.ai import llm


async def test_answer_question_with_chat_and_brain_evidence(monkeypatch):
    seen = {}

    async def create(**kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Friday."))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    evidence = [
        {"kind": "segment", "call_id": "c1", "caller": "Sara", "date": "2026-10-01",
         "t_start": 3.0, "speaker": "caller", "text": "invoice soon"},
        {"kind": "chat", "chat": "sara", "name": "Sara", "sender": "Sara",
         "date": "2026-10-01", "text": "the invoice is due friday"},
        {"kind": "brain", "chat": "sara", "name": "Sara", "date": "", "text": "Sara invoices monthly."},
    ]
    assert await llm.answer_question("when is the invoice due?", evidence) == "Friday."
    assert "the invoice is due friday" in seen["prompt"]
    assert "Sara invoices monthly." in seen["prompt"]
