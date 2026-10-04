# MyPhathom — Plan

A local assistant that picks up WhatsApp calls on your account (Daniyal Areeb), records and transcribes them, can talk to the caller, and later answers questions like "what did Ali say about the deadline?"

> Detailed build instructions for a coding model: see **[SPEC.md](SPEC.md)**.

**Cost target: $0.** Everything runs on this laptop. The heavy work (speech-to-text, LLM, text-to-speech) goes to free APIs, so your machine only moves audio around.

---

## 1. How it works

```
  phathom start  (you "connect" it)
        │
        ▼
┌──────────────────────────┐   incoming call    ┌─────────────────────────┐
│ WhatsApp Web (Chrome,    │ ─────────────────▶ │ Call Watcher            │
│ your account, Playwright)│ ◀── click Accept ─ │ (allowlist, on/off)     │
└──────────┬───────────────┘                    └─────────────────────────┘
   speaker │  ▲ mic
           ▼  │
┌──────────────────────────────────────────────┐
│ PipeWire virtual devices                     │
│  phathom_speaker  → what the caller says     │
│  phathom_mic      ← what the bot says        │
└──────────┬───────────────────────▲───────────┘
           ▼                       │
   Recorder + VAD (speech chunks)  │ TTS audio (edge-tts / Groq TTS)
           │                       │
           ▼                       │
   Groq Whisper (STT) ──▶ Voice Agent (Groq LLM) ── decides if/what to say
           │
           ▼
   SurrealDB: calls, transcript segments, summaries
           │
           ▼
   phathom ask "..."  /  phathom calls  /  desktop notification after each call
```

**"Joins as you":** WhatsApp Web is logged into *your* account (QR scan once). When it answers, the caller sees **your** name and photo. No separate bot account is needed.

---

## 2. Tech stack (all free)

| Job | Choice | Why |
|---|---|---|
| Control WhatsApp | **Playwright (Python) + Chrome** with a saved profile | Scan the QR once, stays logged in |
| Audio routing | **PipeWire null sinks / virtual source** (`pactl`) | Already on your system; separates caller audio from the bot's voice |
| Voice detection | **webrtcvad** | Tiny and CPU-cheap; cuts audio into utterances |
| Speech-to-text | **Groq `whisper-large-v3-turbo`** | Fast and on the free tier |
| Brain (replies + summaries) | **Groq `llama-3.3-70b-versatile`** (`llama-3.1-8b-instant` for fast replies) | Free tier, low latency |
| Text-to-speech | **edge-tts** (free, no key, has English + Urdu voices); Groq `canopylabs/orpheus-v1-english` as English fallback | No local model needed |
| Storage + search | **SurrealDB** (local `surreal start surrealkv://data`) with a full-text index | Free; no embeddings needed at first |
| CLI | **Typer** | `phathom start / stop / status / calls / ask` |
| Notifications | `notify-send` | "Summary ready: call with Ali (12 min)" |

Free-tier note: Groq limits requests and audio-seconds per hour/day. That's plenty for personal calls, but check the current numbers in the Groq console.

---

## 3. Modes (switch from the CLI)

| Mode | What happens when a call comes in |
|---|---|
| `off` | Nothing. You take calls yourself. |
| `notes` | Auto-answers, plays a short disclosure, stays silent, records, then summarizes. |
| `assistant` (default) | Auto-answers, greets the caller, holds a short conversation ("Daniyal is busy, I'm his assistant, what's it about?"), answers simple questions from your **briefing**, takes a message, ends politely. |

**Briefing** (`phathom brief "..."` or `brief.md`): what the bot may say today, for example "I'm in exams until 5pm. If Ahmed calls about the project, say I'll send the doc tonight." Per-contact notes go in `contacts.yaml`.

**Disclosure is on by default.** The bot says it is Daniyal's AI assistant and that the call is being noted. That keeps you on the right side of recording-consent laws and avoids deceiving people. I'm not planning voice cloning.

---

## 4. Project layout

```
MyPhathom/
├── .env                  # GROQ_API_KEY, SURREAL_*, MODE, OWNER_NAME="Daniyal Areeb"
├── brief.md              # today's instructions for the bot
├── contacts.yaml         # allowlist / blocklist + per-person notes
├── phathom/
│   ├── cli.py            # start | stop | status | mode | brief | calls | show | ask
│   ├── config.py
│   ├── whatsapp/
│   │   ├── driver.py     # launch Chrome profile, login check, detect call, accept, hang up
│   │   └── selectors.py  # all DOM selectors in ONE file (WhatsApp updates break these)
│   ├── audio/
│   │   ├── devices.py    # create/remove phathom_speaker + phathom_mic via pactl
│   │   ├── recorder.py   # capture → 16 kHz chunks + full call .wav
│   │   ├── vad.py
│   │   └── player.py     # play TTS into phathom_mic
│   ├── ai/
│   │   ├── stt.py        # Groq Whisper
│   │   ├── llm.py        # Groq chat (reply, summarize, answer questions)
│   │   └── tts.py        # edge-tts (+ Groq TTS fallback)
│   ├── agent.py          # live conversation loop (turn-taking, barge-in, end-of-call)
│   ├── pipeline.py       # after the call: transcript → summary → store → notify
│   └── store.py          # SurrealDB access
└── data/
    ├── chrome-profile/
    ├── recordings/
    └── surreal/
```

---

## 5. Data model (SurrealDB)

```sql
DEFINE TABLE call SCHEMAFULL;
  -- contact, started_at, ended_at, duration_s, mode, audio_path,
  -- summary, action_items[], message_for_owner, sentiment
DEFINE TABLE segment SCHEMAFULL;
  -- call -> record<call>, t_start, t_end, speaker ('caller' | 'phathom'), text
DEFINE ANALYZER phathom_text TOKENIZERS blank, class, punct FILTERS lowercase, ascii;
DEFINE INDEX seg_text ON segment FIELDS text FULLTEXT ANALYZER phathom_text BM25;
DEFINE INDEX call_sum ON call FIELDS summary FULLTEXT ANALYZER phathom_text BM25;
```

Speaker labels come for free: caller audio and bot audio are on separate devices. In **group calls**, all other participants count as "caller" (no diarization in v1).

`phathom ask "what did Ali want?"` runs a full-text search over segments and summaries, takes the top matches plus call metadata, and has Groq answer with call dates as citations.

---

## 6. Live talking loop (assistant mode)

1. Accept the call, wait about 1s, then speak the greeting (pre-generated TTS, so it's instant).
2. VAD detects that the caller has stopped talking (about 700ms of silence) and sends the utterance to Groq Whisper.
3. The LLM gets the briefing, contact notes and conversation so far, and returns `{say, end_call, message_for_owner}`.
4. TTS turns `say` into audio and plays it into `phathom_mic`.
5. **Barge-in:** if the caller starts talking while the bot is speaking, playback stops.
6. The call ends on `end_call`, when the caller hangs up, or at a max length (default 10 min). Then the after-call pipeline runs.

Expected reply delay: **about 1.5–3s** (STT ~0.3s + LLM ~0.5s + TTS ~0.7s + VAD wait). It feels like a slightly slow human, which is fine for an assistant.

---

## 7. Build phases

| # | Phase | Done when |
|---|---|---|
| **0** | **Feasibility check** (highest risk, do first) | WhatsApp Web in Chrome on this laptop can make/receive a call, and we found the accept/hang-up selectors. **If not:** run WhatsApp Desktop in a free Windows VM (VirtualBox) and drive it with the same audio approach. |
| 1 | Audio plumbing | Virtual devices are created; a test call to your own number is recorded with caller and bot on separate tracks. |
| 2 | After-call pipeline | Recording → Groq transcript → summary → SurrealDB → desktop notification. |
| 3 | Auto-join daemon | `phathom start` answers incoming calls on its own, respects the allowlist and `off` mode, and detects hang-up. |
| 4 | Ask/search | `phathom calls`, `phathom show <id>`, `phathom ask "..."` work. |
| 5 | Talking assistant | Greeting, conversation loop, barge-in, message-taking, briefing. |
| 6 | Polish | systemd user service (starts at login), simple local web page for history, retry and back-off on Groq rate limits. |

---

## 8. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| WhatsApp Web calling not available on Linux/your account | WhatsApp officially supports Web calling on Linux (Chrome/Edge/Firefox) as of 2026, but Phase 0 still checks it first; Windows VM fallback. Group calls are out of scope for v1. |
| WhatsApp UI changes break automation | All selectors in `selectors.py`; a health check at startup warns you |
| Phone answers before the bot | When Phathom is on, mute/ignore calls on the phone |
| WhatsApp ToS / account ban | Small risk; no mass actions, normal human-like clicking, only incoming calls |
| Recording consent | Spoken disclosure on every call (on by default) |
| Groq free-tier limits | Chunked audio, retries, model fallback (70b → 8b) |
| Weak laptop | No local models; only audio I/O and a headless-ish Chrome |

---

## 9. What you need to provide

1. A **Groq API key** (console.groq.com, free).
2. Install **SurrealDB** (`curl -sSf https://install.surrealdb.com | sh`).
3. Scan the WhatsApp Web QR code once.
4. Your first `brief.md` and any contacts to allow or block.
