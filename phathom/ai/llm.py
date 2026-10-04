"""Groq chat (summaries, Q&A, live replies) — SPEC §9.8."""

import json
import logging
from dataclasses import dataclass
from typing import Literal

import groq
from pydantic import BaseModel, Field

from phathom.ai.groq_client import get_client, with_retry
from phathom.ai.prompts import live_system_prompt, qa_prompt, summary_prompt
from phathom.config import settings

log = logging.getLogger("phathom.ai.llm")


class AgentDecision(BaseModel):
    say: str = Field(max_length=400)
    lang: Literal["en", "ur"]
    end_call: bool = False
    message_for_owner: str | None = None


class ActionItem(BaseModel):
    owner: str
    item: str
    due: str | None = None


class CallSummary(BaseModel):
    title: str = Field(max_length=80)
    language: str
    summary: str
    key_points: list[str]
    action_items: list[ActionItem]
    message_for_owner: str | None
    follow_up_needed: bool


@dataclass
class LiveContext:
    brief: str
    caller: str
    contact_notes: str | None
    now: str


@dataclass
class Turn:
    role: Literal["caller", "bot"]
    text: str  # caller: raw text; bot: the previous decision JSON


async def live_reply(ctx: LiveContext, history: list[Turn]) -> AgentDecision:
    """One live turn — SPEC §9.8/§10.1. Raises on failure; the agent falls back.

    Deviation: no response_format=json_object here. gpt-oss-20b's constrained
    mode fails Groq-side validation on long prompts (json_validate_failed with
    empty generation), so we instruct + parse robustly instead.
    """
    import re
    messages = [{"role": "system", "content": live_system_prompt(
        ctx.brief, ctx.caller, ctx.contact_notes, ctx.now)}]
    for t in history:
        if t.role == "caller":
            messages.append({"role": "user", "content": f"Caller: {t.text}"})
        else:
            messages.append({"role": "assistant", "content": t.text})
    client = get_client()
    model = settings.LLM_MODEL_LIVE

    def parse(text: str) -> AgentDecision:
        text = (text or "").strip()
        try:
            return AgentDecision(**json.loads(text))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return AgentDecision(**json.loads(m.group(0)))
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")

    async def _call(msgs):
        resp = await client.chat.completions.create(
            model=model, messages=msgs, temperature=0.4, max_tokens=350)
        return (resp.choices[0].message.content or "").strip()

    try:
        return parse(await with_retry(lambda: _call(messages), what=f"llm:{model}"))
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("live_reply parse failed (%r); retrying once", e)
        retry_messages = messages + [{"role": "user", "content":
            "Your last reply was not a single JSON object. Reply with ONLY the JSON object."}]
        return parse(await with_retry(lambda: _call(retry_messages), what=f"llm-retry:{model}"))


async def _chat_json(model: str, messages: list[dict], temperature: float,
                     max_tokens: int, fallback_model: str | None) -> dict:
    """Chat with response_format json_object; retry once with the error appended
    on parse/validation failure (SPEC §9.8)."""
    client = get_client()
    current = model
    try:
        resp = await with_retry(
            lambda: client.chat.completions.create(
                model=current, messages=messages, temperature=temperature,
                max_tokens=max_tokens, response_format={"type": "json_object"}),
            what=f"llm:{current}")
    except groq.RateLimitError:
        if fallback_model and fallback_model != current:
            log.warning("LLM 429 on %s; falling back once to %s", current, fallback_model)
            current = fallback_model
            resp = await with_retry(
                lambda: client.chat.completions.create(
                    model=current, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, response_format={"type": "json_object"}),
                what=f"llm:{current}")
        else:
            raise
    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("LLM bad JSON from %s (%r); retrying once with the error appended", current, e)
        retry_messages = messages + [{"role": "user",
                                      "content": f"Your last reply was not valid JSON: {e}. Reply with ONLY the JSON object."}]
        resp2 = await with_retry(
            lambda: client.chat.completions.create(
                model=current, messages=retry_messages, temperature=temperature,
                max_tokens=max_tokens, response_format={"type": "json_object"}),
            what=f"llm-retry:{current}")
        return json.loads(resp2.choices[0].message.content or "")


def _chunk(transcript: str, size: int = 15000) -> list[str]:
    return [transcript[i:i + size] for i in range(0, len(transcript), size)] or [""]


async def summarize(call_meta: dict, transcript: str) -> CallSummary:
    """Summarize a call. Over ~60k chars: chunk-summarise first (SPEC §10.3)."""
    model = settings.LLM_MODEL_SMART
    fallback = settings.LLM_MODEL_LIVE if settings.LLM_MODEL_LIVE != model else None
    if len(transcript) > 60000:
        parts = []
        for ch in _chunk(transcript):
            data = await _chat_json(model, [
                {"role": "user", "content": summary_prompt(
                    call_meta.get("caller", "?"), call_meta.get("started_at", "?"),
                    call_meta.get("duration", "?"), call_meta.get("mode", "?"), ch)}],
                0.2, settings.SUMMARY_MAX_TOKENS, fallback)
            parts.append(CallSummary(**data).summary)
        transcript = "\n\n".join(f"Part summary: {p}" for p in parts)
    data = await _chat_json(model, [
        {"role": "user", "content": summary_prompt(
            call_meta.get("caller", "?"), call_meta.get("started_at", "?"),
            call_meta.get("duration", "?"), call_meta.get("mode", "?"), transcript)}],
        0.2, 900, fallback)
    return CallSummary(**data)


async def answer_question(question: str, evidence: list[dict]) -> str:
    """Q&A over past calls — SPEC §9.10/§10.4. Plain text, short, cited."""
    from datetime import datetime, timezone
    from phathom.pipeline import mmss
    lines = []
    for e in evidence:
        if e.get("kind") == "segment":
            lines.append(f"- [{e['call_id']}] {e['caller']} ({e['date']}): "
                         f"[{mmss(e['t_start'])}] {e['speaker']}: {e['text']}")
        elif e.get("kind") == "chat":
            lines.append(f"- [chat: {e['name']}] ({e['date']}) {e.get('sender', '')}: {e['text']}")
        elif e.get("kind") == "brain":
            lines.append(f"- [chat: {e['name']}] what we know: {e['text']}")
        else:
            lines.append(f"- [{e['call_id']}] {e['caller']} ({e['date']}): "
                         f"{e.get('title', '')} — {e.get('summary', '')}")
    client = get_client()
    model = settings.LLM_MODEL_SMART
    fallback = settings.LLM_MODEL_LIVE if settings.LLM_MODEL_LIVE != model else None
    messages = [{"role": "user", "content": qa_prompt(
        question, "\n".join(lines) or "(no evidence)",
        datetime.now(timezone.utc).date().isoformat())}]

    async def _call():
        resp = await client.chat.completions.create(
            model=model, messages=messages, temperature=0.2, max_tokens=500)
        return (resp.choices[0].message.content or "").strip()

    try:
        return await with_retry(_call, what=f"llm:{model}")
    except groq.RateLimitError:
        if fallback:
            log.warning("LLM 429 on %s; falling back once to %s", model, fallback)

            async def _fb():
                resp = await client.chat.completions.create(
                    model=fallback, messages=messages, temperature=0.2, max_tokens=500)
                return (resp.choices[0].message.content or "").strip()

            return await with_retry(_fb, what=f"llm:{fallback}")
        raise
