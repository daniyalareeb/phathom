# MyPhathom — Implementation Spec

> **Audience:** an AI coding model (or developer) building MyPhathom from scratch.
> **Companion doc:** `PLAN.md` explains the *why*; this file is the *how*. If they conflict, this file wins.

---

## 0. Rules for the implementing model (read first, follow always)

1. **Build one phase at a time, in order.** Do not start phase N+1 until every acceptance check of phase N passes. Then report the results to the human.
2. **Stop and ask the human** wherever this spec says `🛑 CHECKPOINT`. Never guess past one.
3. **Never invent WhatsApp DOM selectors.** They must be discovered on the live page (Phase 0) and stored only in `phathom/whatsapp/selectors.py`.
4. **Verify library APIs against the installed version** before using them (`python -c "import x; help(x.Y)"` or the official docs). Version-sensitive items are marked ⚠️.
5. **Don't add features, dependencies or files that aren't in this spec.** If something seems missing, ask.
6. **No local ML models.** The user's laptop is weak. All STT, LLM and TTS go through APIs (Groq / edge-tts). Only `webrtcvad` runs locally.
7. **Never commit secrets.** `.env` and `data/` go in `.gitignore`.
8. **Log everything** with the `logging` module to `data/logs/phathom.log` (rotating, 5 × 2 MB) and to the console at INFO.
9. When a step fails, report the **exact command, the full error output, and what you tried**. Don't silently work around it.
10. Use **asyncio** throughout (Playwright async API, `AsyncGroq`, async SurrealDB client, `asyncio.create_subprocess_exec`). No threads unless a library forces it.

---

## 1. Environment facts (verified on the target machine)

| Item | Value |
|---|---|
| OS | Linux (Ubuntu-like), kernel 6.8, PipeWire with `pipewire-pulse` |
| Python | 3.10.12 (`/usr/bin/python3`) |
| Present | `ffmpeg`, `pactl`, `pw-record` |
| GPU | none → no local models |
| Project root | `/home/daniyalareeb/MyProjects/MyPhathom` |
| Owner name | `Daniyal Areeb` |
| Call languages | English, Urdu, and mixed Urdu-English. Support all three. |

External facts (checked October 2026; re-check ⚠️ items):
- WhatsApp Web supports voice/video calls on Linux in Chrome, Edge and Firefox. **1:1 calls are the target.** Group-call support on Web may be limited; treat group calls as out of scope for v1.
- ⚠️ Groq STT models: `whisper-large-v3-turbo` (fast, live use) and `whisper-large-v3` (after-call quality). Endpoint: `client.audio.transcriptions.create(...)`.
- ⚠️ Groq LLMs: `llama-3.3-70b-versatile` (summaries, Q&A) and `llama-3.1-8b-instant` (live replies). Before coding, list available models with `client.models.list()`; if one is missing, pick the closest and tell the human.
- ⚠️ Groq TTS: `canopylabs/orpheus-v1-english` (English only) via `client.audio.speech.create(model, voice, input, response_format="wav")`.
- ⚠️ SurrealDB 3.x: full-text index syntax is `FULLTEXT ANALYZER x BM25` (the old `SEARCH ANALYZER` was removed). Match operator: `field @N@ $q`, score: `search::score(N)`.
- edge-tts is an **unofficial** client for Microsoft's free read-aloud voices. It needs no key, but it can break; Groq TTS is the fallback for English.

---

## 2. Architecture

### 2.1 Processes

| Process | Started by | Lifetime | Job |
|---|---|---|---|
| `surreal` | `scripts/run_db.sh` or systemd | always on | database on `127.0.0.1:8000` |
| `phathom daemon` | `phathom start` | while "connected" | Chrome + WhatsApp, call watcher, live agent, after-call pipeline |
| `phathom <cmd>` | user | one-shot | CLI: talks to the DB and to the daemon via the state file |

### 2.2 Audio routing (PipeWire, through the pulse compatibility layer)

```
 Chrome (WhatsApp Web) output ──▶ [sink] phathom_speaker ──▶ phathom_speaker.monitor ──▶ recorder (caller track)
                                                       └─(optional loopback)──▶ real speakers (LISTEN_LIVE=true)

 TTS playback (paplay) ──▶ [sink] phathom_voice ──▶ phathom_voice.monitor ──▶ [source] phathom_mic ──▶ Chrome mic input
                                                    └──▶ recorder (bot track)
```

- The caller's voice and the bot's voice are on **separate tracks**, so speaker labels come for free.
- The bot never hears itself: TTS goes only to `phathom_voice`, never to `phathom_speaker`.

### 2.3 Call lifecycle (daemon state machine)

```
IDLE ──incoming call detected──▶ RINGING
RINGING ──mode==off or contact blocked──▶ IDLE (do nothing, let it ring)
RINGING ──ring UI disappears before ANSWER_DELAY_S (owner answered on phone / caller hung up)──▶ IDLE
RINGING ──ANSWER_DELAY_S elapsed──▶ click Accept ──▶ IN_CALL
IN_CALL ──call UI ended / end_call / MAX_CALL_S──▶ click Hang up if still connected ──▶ POST_CALL
POST_CALL ──pipeline finished (in background)──▶ IDLE   (the daemon can take a new call while the pipeline runs)
```

---

## 3. Project layout (create exactly this)

```
MyPhathom/
├── PLAN.md / SPEC.md
├── pyproject.toml            # package "phathom", console script: phathom = phathom.cli:app
├── .env.example              # every key from §4 with safe defaults, no real secrets
├── .gitignore                # .env, data/, .venv/, __pycache__/
├── brief.md                  # owner's instructions for today (plain text, read on every call)
├── contacts.yaml             # see §6
├── scripts/
│   ├── run_db.sh             # starts SurrealDB
│   └── audio_setup.sh        # manual debug helper: create / remove the virtual devices
├── phathom/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py             # pydantic-settings Settings, loaded from .env
│   ├── state.py              # data/state.json read/write (mode, pid)
│   ├── logging_setup.py
│   ├── contacts.py
│   ├── daemon.py             # main loop + state machine (§2.3)
│   ├── whatsapp/
│   │   ├── driver.py
│   │   └── selectors.py
│   ├── audio/
│   │   ├── devices.py
│   │   ├── recorder.py
│   │   ├── vad.py
│   │   └── player.py
│   ├── ai/
│   │   ├── groq_client.py    # one shared AsyncGroq + retry wrapper
│   │   ├── stt.py
│   │   ├── llm.py
│   │   ├── tts.py
│   │   └── prompts.py        # ALL prompt text lives here (§10)
│   ├── agent.py              # live conversation loop
│   ├── pipeline.py           # after-call processing
│   └── store.py              # SurrealDB access + schema bootstrap
├── tests/
│   ├── test_vad.py
│   ├── test_llm_parsing.py
│   ├── test_store.py
│   ├── test_contacts.py
│   └── fixtures/             # short sample wavs (generated with edge-tts in a test helper)
└── data/                     # git-ignored; created at runtime
    ├── chrome-profile/
    ├── recordings/<call_id>/{caller.wav, bot.wav, mixed.mp3}
    ├── surreal/
    ├── tts-cache/
    ├── debug/                # screenshots + DOM dumps from Phase 0 and from failures
    ├── logs/
    └── state.json
```

---

## 4. Configuration (`.env`, loaded by `phathom/config.py`)

| Key | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | — (required) | Groq key |
| `OWNER_NAME` | `Daniyal Areeb` | used in greetings and prompts |
| `OWNER_SHORT_NAME` | `Daniyal` | spoken name |
| `DEFAULT_MODE` | `assistant` | `off` \| `notes` \| `assistant` |
| `ANSWER_DELAY_S` | `6` | seconds of ringing before the bot answers (gives the owner time to pick up) |
| `MAX_CALL_S` | `600` | hard limit per call |
| `SILENCE_HANGUP_S` | `45` | hang up after this much two-way silence |
| `LISTEN_LIVE` | `false` | also play caller audio on the real speakers |
| `DISCLOSURE` | `true` | speak the AI/recording notice at the start. **Must default to true.** |
| `STT_MODEL_LIVE` | `whisper-large-v3-turbo` | |
| `STT_MODEL_FINAL` | `whisper-large-v3` | |
| `LLM_MODEL_LIVE` | `llama-3.1-8b-instant` | |
| `LLM_MODEL_SMART` | `llama-3.3-70b-versatile` | |
| `TTS_ENGINE` | `edge` | `edge` \| `groq` |
| `TTS_VOICE_EN` | `en-US-AndrewNeural` | edge-tts voice |
| `TTS_VOICE_UR` | `ur-PK-AsadNeural` | edge-tts voice |
| `GROQ_TTS_VOICE` | `troy` | used when `TTS_ENGINE=groq` or edge fails (English only) |
| `SURREAL_URL` | `ws://127.0.0.1:8000/rpc` | |
| `SURREAL_USER` / `SURREAL_PASS` | `root` / `root` | local only; the server binds to 127.0.0.1 |
| `SURREAL_NS` / `SURREAL_DB` | `phathom` / `main` | |
| `CHROME_CHANNEL` | `chrome` | Playwright channel; use `chromium` if Google Chrome isn't installed |
| `KEEP_AUDIO_DAYS` | `30` | delete recordings older than this (transcripts stay) |

---

## 5. Dependencies

```
playwright        # then: playwright install chromium   (also works with installed Google Chrome)
groq
surrealdb         # ⚠️ official Python SDK; check AsyncSurreal API for the installed version
edge-tts
webrtcvad-wheels  # prebuilt webrtcvad (the plain 'webrtcvad' package needs a C compiler)
typer
rich
pydantic-settings
python-dotenv
pyyaml
tenacity
pytest, pytest-asyncio   # dev
```

System: `pulseaudio-utils` (provides `parec`, `paplay`, `pacat`; install with apt if `which parec` fails), `libnotify-bin` (`notify-send`), SurrealDB binary (`curl -sSf https://install.surrealdb.com | sh`).

Use a venv: `python3 -m venv .venv && . .venv/bin/activate && pip install -e .[dev]`.

---

## 6. `contacts.yaml`

```yaml
# policy: "all" = answer everyone not blocked; "allowlist" = answer only listed contacts
policy: all
block:
  - "Unknown"          # matches the caller label shown by WhatsApp
allow: []
notes:                 # free-text context the bot may use, keyed by the name WhatsApp shows
  "Ahmed Uni": "Classmate on the FYP. Owner promised to send the report draft."
  "Ammi": "Mother. Be warm, speak Urdu, always take a message."
```

Matching is a case-insensitive exact match on the caller label read from the call UI. `contacts.py` exposes:

```python
def should_answer(caller_label: str, mode: str) -> bool
def notes_for(caller_label: str) -> str | None
```

---

## 7. State file and CLI

### 7.1 `data/state.json`

```json
{ "mode": "assistant", "daemon_pid": 12345, "started_at": "2026-10-02T10:00:00Z", "current_call": null }
```

- Write atomically (write to a temp file, then `os.replace`).
- The daemon re-reads `mode` **on every incoming call**, so `phathom mode notes` takes effect without a restart.
- The daemon sets `current_call` to `{ "id", "caller", "since" }` during a call and back to `null` afterwards.

### 7.2 Commands (`phathom/cli.py`, Typer)

| Command | Behaviour |
|---|---|
| `phathom setup` | Create `data/` dirs, copy `.env.example` to `.env` if missing, check `parec`/`paplay`/`ffmpeg`/`surreal`/Chrome exist, test the Groq key with `models.list()`, bootstrap the DB schema. Print a ✅/❌ table. |
| `phathom login` | Open WhatsApp Web in the persistent profile, **headed**, and wait until the chat list is visible (QR scanned). Then close. |
| `phathom start` | Run the daemon in the foreground (Ctrl+C stops it). Refuse if `daemon_pid` is alive. `--background` forks with `nohup` and writes the pid. |
| `phathom stop` | SIGTERM the `daemon_pid`; wait up to 10s; clear the pid. |
| `phathom status` | Mode, daemon alive?, current call, WhatsApp logged in? (as last reported by the daemon), last 3 calls. |
| `phathom mode <off\|notes\|assistant>` | Update `state.json`. |
| `phathom brief [TEXT]` | No arg: print `brief.md`. With arg: overwrite `brief.md`. `--append` appends a line. |
| `phathom calls [--limit 20]` | Table: id, date, caller, duration, mode, title. |
| `phathom show <call_id>` | Summary, action items, message for owner, full transcript with speaker labels and `mm:ss` times. |
| `phathom ask "<question>"` | Q&A over all calls (§9.10). |
| `phathom test-audio` | Phase 1 self-test (§11, Phase 1). |
| `phathom process <wav_path> [--caller NAME]` | Run the after-call pipeline on any wav file (for testing without a real call). |

### 7.3 Daemon shutdown
On SIGTERM/SIGINT: if in a call, hang up; let a running pipeline finish (max 120s); unload the audio modules this run loaded; close the browser; clear `daemon_pid`.

---

## 8. Data model (SurrealDB 3.x) — `store.py` runs this on startup (idempotent)

```sql
DEFINE NAMESPACE IF NOT EXISTS phathom;
USE NS phathom DB main;
DEFINE DATABASE IF NOT EXISTS main;

DEFINE TABLE IF NOT EXISTS call SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS caller          ON call TYPE string;
DEFINE FIELD IF NOT EXISTS started_at      ON call TYPE datetime;
DEFINE FIELD IF NOT EXISTS ended_at        ON call TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS duration_s      ON call TYPE option<int>;
DEFINE FIELD IF NOT EXISTS mode            ON call TYPE string ASSERT $value IN ['notes','assistant','manual'];
DEFINE FIELD IF NOT EXISTS status          ON call TYPE string ASSERT $value IN ['live','processing','done','failed'];
DEFINE FIELD IF NOT EXISTS audio_dir       ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS language        ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS title           ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS summary         ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS key_points      ON call TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS action_items    ON call TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS action_items.*.owner ON call TYPE string;
DEFINE FIELD IF NOT EXISTS action_items.*.item  ON call TYPE string;
DEFINE FIELD IF NOT EXISTS action_items.*.due   ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS message_for_owner ON call TYPE option<string>;
DEFINE FIELD IF NOT EXISTS follow_up_needed  ON call TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS error           ON call TYPE option<string>;

DEFINE TABLE IF NOT EXISTS segment SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS call    ON segment TYPE record<call>;
DEFINE FIELD IF NOT EXISTS t_start ON segment TYPE float;   -- seconds from call start
DEFINE FIELD IF NOT EXISTS t_end   ON segment TYPE float;
DEFINE FIELD IF NOT EXISTS speaker ON segment TYPE string ASSERT $value IN ['caller','phathom'];
DEFINE FIELD IF NOT EXISTS text    ON segment TYPE string;
DEFINE FIELD IF NOT EXISTS source  ON segment TYPE string ASSERT $value IN ['live','final'];
DEFINE INDEX IF NOT EXISTS seg_call ON segment FIELDS call;

DEFINE ANALYZER IF NOT EXISTS phathom_text TOKENIZERS blank, class, punct FILTERS lowercase, ascii;
DEFINE INDEX IF NOT EXISTS seg_text_ft ON segment FIELDS text    FULLTEXT ANALYZER phathom_text BM25;
DEFINE INDEX IF NOT EXISTS call_sum_ft ON call    FIELDS summary FULLTEXT ANALYZER phathom_text BM25;
```

Notes:
- No `snowball(english)` filter: transcripts mix Urdu and English, and stemming would damage Urdu words.
- ⚠️ If any statement fails to parse on the installed SurrealDB version, check the docs for that version and report the change to the human. Don't drop features silently.
- Call ids: `call:⟨YYYYMMDD_HHMMSS_<slug(caller)>⟩`, e.g. `call:⟨20261002_143005_ahmed_uni⟩`. The same string is the recordings folder name.

---

## 9. Module specifications

### 9.1 `audio/devices.py`

```python
class AudioDevices:
    async def setup(self) -> None      # idempotent: reuse devices that already exist
    async def teardown(self) -> None   # unload only the module ids THIS process loaded
    async def route_chrome(self) -> bool  # fallback routing, see below; True if streams were moved
```

Commands (run with `asyncio.create_subprocess_exec`; parse the module id from stdout):

```bash
pactl load-module module-null-sink sink_name=phathom_speaker sink_properties=device.description=Phathom_Speaker
pactl load-module module-null-sink sink_name=phathom_voice   sink_properties=device.description=Phathom_Voice
pactl load-module module-remap-source master=phathom_voice.monitor source_name=phathom_mic source_properties=device.description=Phathom_Mic
# only if LISTEN_LIVE=true:
pactl load-module module-loopback source=phathom_speaker.monitor latency_msec=60
```

- Check whether a device already exists with `pactl list short sinks` / `pactl list short sources`.
- **Primary routing:** launch Chrome with env vars `PULSE_SINK=phathom_speaker` and `PULSE_SOURCE=phathom_mic` (§9.2).
- **Fallback routing (`route_chrome`)**, called once a call connects: find Chrome's streams with `pactl -f json list sink-inputs` and `pactl -f json list source-outputs` (match `application.name` containing `Chrome` or `Chromium`), then run `pactl move-sink-input <id> phathom_speaker` and `pactl move-source-output <id> phathom_mic`.
- **Never** change the system default sink or source. The owner's normal audio must keep working.

### 9.2 `whatsapp/driver.py`

```python
@dataclass
class IncomingCall:
    caller: str           # label shown in the ring UI (contact name or phone number)
    is_video: bool
    is_group: bool

class WhatsAppDriver:
    async def start(self, headed: bool = True) -> None
    async def is_logged_in(self) -> bool
    async def wait_for_login(self, timeout_s: int = 300) -> None
    async def poll_incoming_call(self) -> IncomingCall | None   # non-blocking, called every 0.5s
    async def accept(self, audio_only: bool = True) -> None     # for video calls: accept, then turn the camera off
    async def is_call_active(self) -> bool
    async def hang_up(self) -> None
    async def debug_dump(self, tag: str) -> Path   # screenshot + page.content() to data/debug/<ts>_<tag>.{png,html}
    async def close(self) -> None
```

Launch:

```python
ctx = await playwright.chromium.launch_persistent_context(
    user_data_dir="data/chrome-profile",
    channel=settings.CHROME_CHANNEL,
    headless=False,                         # WebRTC calls need a real (headed) browser
    env={**os.environ, "PULSE_SINK": "phathom_speaker", "PULSE_SOURCE": "phathom_mic"},
    permissions=["microphone", "notifications"],
    args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"],
    viewport={"width": 1280, "height": 800},
)
```

- **Do NOT** pass `--use-fake-device-for-media-stream`. It replaces the mic with a test tone.
- Grant permissions for the origin: `await ctx.grant_permissions(["microphone"], origin="https://web.whatsapp.com")`.
- The call UI **may open in a new window or popup**. Listen to `ctx.on("page", ...)` and check every open page when polling. Phase 0 tells you which case applies.
- All selectors come from `selectors.py`. Prefer role/aria-based locators (`page.get_by_role("button", name=re.compile(...))`) over CSS class names, because WhatsApp's class names are obfuscated and change.
- Any unexpected exception in a driver method calls `debug_dump("<method>_error")` before re-raising.
- Every 60s while idle, check `is_logged_in()`. If logged out: `notify-send "Phathom: WhatsApp logged out — run 'phathom login'"`, set state, and keep running (stay idle).

### 9.3 `whatsapp/selectors.py`

Fill it in during Phase 0 with what you actually observed. Template:

```python
# Discovered on: <date>, WhatsApp Web build: <value from page if visible>
# Each entry: (strategy, value). strategy is "role" -> get_by_role("button", name=re.compile(value, re.I)),
# "css" -> locator(value), "text" -> get_by_text(re.compile(value, re.I)).
LOGGED_IN_MARKER   = ("css", "...")   # e.g. the chat list pane
QR_CODE            = ("css", "...")
INCOMING_CALL_ROOT = (...)            # the ring UI container
INCOMING_CALLER    = (...)            # element holding the caller name, inside the root
INCOMING_IS_VIDEO  = (...)            # how to tell video from voice
ACCEPT_BUTTON      = (...)
DECLINE_BUTTON     = (...)
ACTIVE_CALL_ROOT   = (...)            # present while connected
CAMERA_TOGGLE      = (...)
HANGUP_BUTTON      = (...)
```

### 9.4 `audio/recorder.py`

```python
class TrackRecorder:
    """Streams one pulse source into (a) a wav file and (b) an asyncio.Queue of 30ms frames."""
    def __init__(self, source: str, wav_path: Path, frame_queue: asyncio.Queue[bytes] | None)
    async def start(self) -> None
    async def stop(self) -> None   # flush and close the wav (a valid header is required)
```

- Command: `parec -d <source> --format=s16le --rate=16000 --channels=1 --raw` (fallback if `parec` is missing: `ffmpeg -f pulse -i <source> -ac 1 -ar 16000 -f s16le -`).
- Read stdout in chunks of **960 bytes** (30ms at 16 kHz, 16-bit mono). Write each chunk to a `wave` file and, if there is a queue, `put_nowait` it (drop the oldest if the queue is full; max size 200).
- During a call there are two recorders: `phathom_speaker.monitor` → `caller.wav` (with queue) and `phathom_voice.monitor` → `bot.wav` (no queue).
- After the call: `ffmpeg -i caller.wav -i bot.wav -filter_complex amix=inputs=2 -ac 1 -b:a 48k mixed.mp3`.

### 9.5 `audio/vad.py`

```python
@dataclass
class Utterance:
    pcm: bytes          # 16 kHz s16le mono
    t_start: float      # seconds since call start
    t_end: float

class Segmenter:
    def __init__(self, aggressiveness=2, start_frames=6, end_silence_ms=700, min_ms=400, max_ms=20000, preroll_ms=300)
    def feed(self, frame: bytes, t: float) -> list[Utterance] | None
    @property
    def speaking(self) -> bool   # True while an utterance is in progress (used for barge-in)
```

- An utterance starts when at least `start_frames` of the last 10 frames are voiced. Include `preroll_ms` of audio from before the start.
- It ends after `end_silence_ms` of consecutive unvoiced frames, or is force-cut at `max_ms`.
- Drop utterances shorter than `min_ms`.
- Pure Python + webrtcvad, no I/O. Fully unit-testable.

### 9.6 `audio/player.py`

```python
class Player:
    async def play(self, wav_path: Path) -> bool   # True if it finished, False if interrupted
    async def interrupt(self) -> None
    @property
    def playing(self) -> bool
```

Use `paplay --device=phathom_voice <wav>`. `interrupt()` terminates the process. Only one playback at a time: a new `play` interrupts the current one.

### 9.7 `ai/groq_client.py`, `ai/stt.py`, `ai/tts.py`

- One `AsyncGroq` instance. Wrap calls with tenacity: retry on 429 and 5xx, honour the `retry-after` header if present, otherwise exponential back-off (1, 2, 4, 8s), max 4 attempts. On a 429 for a model, log it and fall back once (`LLM_MODEL_SMART` → `LLM_MODEL_LIVE`; `STT_MODEL_FINAL` → `STT_MODEL_LIVE`).

```python
# stt.py
async def transcribe_pcm(pcm: bytes, model: str, prompt: str | None = None) -> str
async def transcribe_file(path: Path, model: str) -> list[dict]   # [{start, end, text}] from response_format="verbose_json"
```

- `transcribe_pcm`: wrap the bytes in an in-memory wav and send `language` **unset** (auto, so Urdu works). Pass `temperature=0` and `prompt=f"Phone call with {OWNER_SHORT_NAME}. Urdu and English."`.
- **Hallucination filter:** Whisper invents text on silence or noise. Drop the result if it's empty, or if the utterance is under 1.0s and the text (lowercased, stripped of punctuation) is in `{"thank you", "thanks for watching", "you", "bye", ".", "شکریہ"}`, or if the same text repeats 3+ times in a row.
- `transcribe_file`: files over 20 MB are split with ffmpeg into 10-minute chunks of 16 kHz mono **flac**. Offset each chunk's timestamps.

```python
# tts.py
async def synth(text: str, lang: str) -> Path   # returns a 48 kHz mono wav, cached by sha1(engine+voice+text)
```

- edge: `edge_tts.Communicate(text, voice).save(mp3)`, then `ffmpeg -y -i mp3 -ar 48000 -ac 1 wav`. Voice: `TTS_VOICE_UR` if `lang=="ur"`, else `TTS_VOICE_EN`.
- If edge fails **and** `lang != "ur"`: use Groq TTS (`GROQ_TTS_VOICE`). If edge fails for Urdu, raise; the agent then says the English fallback line (§10.2).
- Cache in `data/tts-cache/`. Pre-synthesise the greeting and fixed lines when the daemon starts.

### 9.8 `ai/llm.py`

```python
async def live_reply(ctx: LiveContext, history: list[Turn]) -> AgentDecision
async def summarize(call_meta: dict, transcript: str) -> CallSummary
async def answer_question(question: str, evidence: list[dict]) -> str
```

- Use `response_format={"type": "json_object"}` for `live_reply` and `summarize`. Parse into pydantic models (below). On a parse or validation failure: retry once with the error appended to the messages; then fall back (live: §10.2 fallback line; summary: store `status='failed'` with the error).
- `live_reply`: `max_tokens=200`, `temperature=0.4`, model `LLM_MODEL_LIVE`. `summarize` / `answer_question`: model `LLM_MODEL_SMART`, `temperature=0.2`.

```python
class AgentDecision(BaseModel):
    say: str = Field(max_length=400)           # what to speak; "" means stay silent
    lang: Literal["en", "ur"]
    end_call: bool = False
    message_for_owner: str | None = None       # cumulative message so far, overwritten each turn

class ActionItem(BaseModel):
    owner: str; item: str; due: str | None = None

class CallSummary(BaseModel):
    title: str = Field(max_length=80)
    language: str
    summary: str                               # 3–6 sentences, English
    key_points: list[str]
    action_items: list[ActionItem]
    message_for_owner: str | None
    follow_up_needed: bool
```

### 9.9 `agent.py` — live conversation loop (assistant mode)

```python
class LiveAgent:
    def __init__(self, call_id, caller, frame_queue, player, store, settings)
    async def run(self) -> AgentResult   # returns when the call should end or is cancelled
```

Algorithm:
1. `t0 = monotonic()`. Wait 1.0s after connect, then play the greeting (§10.2; pick Urdu or English from the caller's notes, default English). Add it to `history` and save a `segment` (speaker `phathom`, source `live`).
2. Loop: take a frame from the queue → `segmenter.feed(frame, t)`.
   - **Barge-in:** if `player.playing` and `segmenter.speaking` has been true for at least 300ms → `player.interrupt()`.
   - On an `Utterance`: `text = transcribe_pcm(...)`. Skip if filtered. Save the caller segment. Append to `history`.
   - `decision = live_reply(ctx, history)`. If `decision.say`: `synth` → `play`, and save the bot segment (if playback was interrupted, mark the text with a trailing ` [interrupted]`).
   - Keep the latest `message_for_owner`.
   - If `decision.end_call`: wait until playback finishes, then return.
3. **Overlap rule:** process utterances strictly one at a time. If the caller speaks again while STT/LLM is still running for the previous one, merge the new utterance into the pending one (concatenate PCM) instead of answering twice.
4. Stop on: `end_call`, the driver reports the call ended, `MAX_CALL_S`, or `SILENCE_HANGUP_S` with no utterance and no playback. Before a timeout hang-up, play the goodbye line.
5. Target latency (utterance end → audio start) under 3s. Log every turn: `stt_ms`, `llm_ms`, `tts_ms`.

**Notes mode:** no LiveAgent. Play only the disclosure line (if `DISCLOSURE`), then just record until the call ends or `MAX_CALL_S`.

### 9.10 `pipeline.py` — after the call (runs as a background task)

1. Set `status='processing'`. Stop the recorders, build `mixed.mp3`.
2. `transcribe_file(caller.wav, STT_MODEL_FINAL)` → caller segments with `source='final'`. Bot segments: reuse the live `phathom` segments (we know exactly what was said). In notes mode, also transcribe `bot.wav` only if the disclosure was played (or just insert the disclosure text with its known timing).
3. Delete the `live` caller segments for this call and insert the `final` ones. (Final transcription is higher quality.)
4. Build the transcript text: segments sorted by `t_start`, formatted `[mm:ss] Caller: ...` / `[mm:ss] Phathom: ...`.
5. `summarize(...)` → update the call record with the summary fields, `ended_at`, `duration_s`, `status='done'`.
6. Notification: `notify-send "Phathom · <caller> (<m> min)" "<title> — <message_for_owner or first key point>"`.
7. On any exception: `status='failed'`, `error=<repr>`, notify "processing failed". Keep the audio so it can be re-run with `phathom process`.
8. Once a day (first pipeline run after midnight): delete recording folders older than `KEEP_AUDIO_DAYS`.

### 9.11 `phathom ask` — retrieval

```sql
-- $q = the question text
SELECT id, call, t_start, speaker, text, search::score(1) AS score
  FROM segment WHERE text @1@ $q ORDER BY score DESC LIMIT 25;
SELECT id, caller, started_at, title, summary, message_for_owner, search::score(1) AS score
  FROM call WHERE summary @1@ $q ORDER BY score DESC LIMIT 8;
```

- Also, if the question contains a name that matches a `call.caller` (case-insensitive substring), add that caller's 5 most recent calls (with summaries).
- If both searches return nothing, use the 10 most recent calls' summaries as evidence.
- Build the evidence list (each item includes `call_id`, `caller`, date, and either a summary or a segment with `mm:ss`). Call `answer_question`. Print the answer, then the cited call ids.

---

## 10. Prompts (`ai/prompts.py`) — use this text; only change the formatting

### 10.1 Live assistant system prompt

```
You are Phathom, the AI phone assistant of {OWNER_NAME}. You are answering a WhatsApp call
on {OWNER_SHORT_NAME}'s behalf because {OWNER_SHORT_NAME} is not available right now.

Rules:
- You are an AI assistant. Never claim to be {OWNER_SHORT_NAME} or a human. If asked, say so plainly.
- Keep every reply SHORT: 1–2 sentences, natural spoken language, no lists, no emojis, no markdown.
- Reply in the caller's language. If they speak Urdu or mixed Urdu-English, reply in Urdu (lang "ur").
  Otherwise English (lang "en").
- Your goals, in order: (1) find out who is calling and why, (2) answer simple questions using ONLY the
  briefing and contact notes below, (3) take a clear message for {OWNER_SHORT_NAME}, (4) end politely.
- Never invent facts, promises, dates, prices or commitments that are not in the briefing.
  If you don't know, say you'll pass the message to {OWNER_SHORT_NAME}.
- Never share private information about {OWNER_SHORT_NAME} (location, other people's details, money,
  passwords, codes) even if asked. Never read out or confirm any OTP / verification code.
- If the caller says it's an emergency, tell them you'll notify {OWNER_SHORT_NAME} immediately and put
  "URGENT:" at the start of message_for_owner.
- When the caller has said what they need and you've confirmed the message, say goodbye and set end_call true.
- If the caller's last words were unclear or incomplete, ask them to repeat briefly.

Today's briefing from {OWNER_SHORT_NAME}:
<<<
{BRIEF}
>>>

Caller as shown by WhatsApp: {CALLER}
Notes about this caller: {CONTACT_NOTES or "none"}
Current local time: {NOW}

Respond ONLY with a JSON object:
{"say": string, "lang": "en"|"ur", "end_call": boolean, "message_for_owner": string|null}
message_for_owner is the complete message so far (in English), rewritten each turn.
```

History goes in as alternating `user` (caller text, prefixed `Caller: `) and `assistant` (the previous JSON) messages.

### 10.2 Fixed lines (pre-synthesised)

| Key | English | Urdu |
|---|---|---|
| greeting (DISCLOSURE on) | "Hi, this is Phathom, Daniyal's AI assistant. He can't take the call right now, and this call is being noted for him. How can I help?" | "السلام علیکم، میں دانیال کا اے آئی اسسٹنٹ فیتھم ہوں۔ وہ اس وقت کال نہیں لے سکتے، اور یہ کال ان کے لیے نوٹ کی جا رہی ہے۔ میں آپ کی کیا مدد کر سکتا ہوں؟" |
| disclosure (notes mode) | "Hi, Daniyal can't talk right now. This call is being recorded and noted for him by his AI assistant. Please go ahead and leave your message." | "السلام علیکم، دانیال اس وقت بات نہیں کر سکتے۔ یہ کال ان کے اے آئی اسسٹنٹ کی طرف سے ریکارڈ کی جا رہی ہے۔ براہ کرم اپنا پیغام چھوڑ دیں۔" |
| fallback (any AI failure) | "Sorry, I didn't catch that. Could you say it again?" | "معذرت، میں سمجھ نہیں سکا۔ کیا آپ دوبارہ کہیں گے؟" |
| goodbye (timeouts) | "I'll pass your message to Daniyal. Goodbye!" | "میں آپ کا پیغام دانیال تک پہنچا دوں گا۔ خدا حافظ!" |

Names in these lines come from settings (`OWNER_SHORT_NAME`), not hard-coded. If the fallback line fails 3 times in a row in one call, play goodbye and end the call.

### 10.3 Summary prompt

```
You summarise a phone call for {OWNER_NAME}. "Phathom" is his AI assistant that answered the call;
"Caller" is the other person. The transcript may mix Urdu and English and may contain
transcription errors — interpret sensibly, don't invent anything that isn't supported.

Write everything in English. Respond ONLY with JSON:
{"title": str (<=80 chars), "language": str, "summary": str (3-6 sentences),
 "key_points": [str], "action_items": [{"owner": str, "item": str, "due": str|null}],
 "message_for_owner": str|null, "follow_up_needed": bool}

Call: caller={CALLER}, started={STARTED_AT}, duration={DURATION}, mode={MODE}
Transcript:
{TRANSCRIPT}
```

If the transcript is over ~60k characters, summarise it in 15k-character chunks first, then summarise the chunk summaries with the same JSON schema.

### 10.4 Q&A prompt

```
You answer {OWNER_NAME}'s questions about his past phone calls using ONLY the evidence below.
Cite calls as [call_id]. If the evidence doesn't contain the answer, say so plainly.
Be concise. Current date: {TODAY}.

Evidence:
{EVIDENCE}

Question: {QUESTION}
```

---

## 11. Build phases (do them in order)

Testing needs a **second WhatsApp account** (a friend or a second phone) that can call the owner. Whenever a step says "test call", ask the human to place it and wait for confirmation.

### Phase 0 — Feasibility and selector discovery
**Goal:** prove that WhatsApp Web in Playwright-controlled Chrome can receive and answer a call, and collect the selectors.

1. Create the skeleton: `pyproject.toml`, `.gitignore`, `.env.example`, `phathom/config.py`, `phathom/logging_setup.py`, an empty `selectors.py`. Set up the venv and install the deps.
2. Write a throwaway script `scripts/discover.py`: launch the persistent context (§9.2 settings, headed), open `https://web.whatsapp.com`, and wait for login.
3. 🛑 CHECKPOINT: ask the human to scan the QR code. Wait.
4. Confirm a chat shows voice/video call buttons. Save `debug_dump("logged_in")`.
5. 🛑 CHECKPOINT: ask the human to place a **voice** test call. While it rings, every 1s for 30s: dump screenshots and HTML from **every open page** in the context, and log new pages/popups.
6. From the dumps, identify the ring UI, the caller label, and the accept / decline buttons. Write candidates to `selectors.py`.
7. 🛑 CHECKPOINT: second test call. The script must detect it, print the caller label, and click Accept. Then dump the in-call UI and identify `ACTIVE_CALL_ROOT` and `HANGUP_BUTTON`. After 10s, click hang-up.
8. Repeat 5–7 with a **video** call to find `INCOMING_IS_VIDEO` and `CAMERA_TOGGLE`.

**Acceptance:**
- [ ] The bot answers a test call by itself and the caller sees it connect; it hangs up by itself.
- [ ] `selectors.py` is filled in with comments saying where each was found.
- [ ] Report: does the call UI open in the same page, a popup, or a new window?

**If WhatsApp Web shows no call buttons or calls fail in Playwright's Chrome:** try `CHROME_CHANNEL=chrome` (real Google Chrome), then a normal non-automated Chrome window to rule out automation. If it still fails, 🛑 STOP and report to the human. The fallback (WhatsApp Desktop in a Windows VM) is a different project shape and needs their decision.

### Phase 1 — Audio plumbing
1. Implement `devices.py`, `recorder.py`, `player.py`, and `scripts/audio_setup.sh` (create/remove, for debugging).
2. `phathom test-audio`: set up devices → start recorders on both monitors → generate a test wav with ffmpeg (`sine=frequency=440:duration=3`) → play it to `phathom_voice` → play another to `phathom_speaker` with `paplay --device=phathom_speaker` → stop → check levels with `ffmpeg -af volumedetect`.
3. Add Chrome routing to the driver (env vars + `route_chrome` fallback).
4. 🛑 CHECKPOINT: test call. The bot answers, plays a TTS sentence (edge-tts, made ad hoc) into `phathom_mic`, and records 20s. The human talks during the call.

**Acceptance:**
- [ ] `test-audio`: `bot.wav` has the tone only where it was sent to `phathom_voice`; `caller.wav` has only the speaker tone (mean volume > -40 dB where present, < -60 dB elsewhere).
- [ ] Test call: the caller **heard** the bot's sentence (ask the human), and `caller.wav` contains the caller's voice (send the file to STT and show the text).
- [ ] The owner's normal speakers and mic still work after `teardown()`; the default sink/source are unchanged (compare `pactl get-default-sink` before and after).

### Phase 2 — After-call pipeline and storage
1. `scripts/run_db.sh`: `surreal start --bind 127.0.0.1:8000 --user $SURREAL_USER --pass $SURREAL_PASS surrealkv://data/surreal` ⚠️ check flags with `surreal start --help`.
2. Implement `store.py` (schema bootstrap from §8, CRUD helpers), `stt.py`, `llm.summarize`, `prompts.py`, `pipeline.py`.
3. Implement `phathom setup`, `phathom process <wav>`, `phathom calls`, `phathom show`.

**Acceptance:**
- [ ] `phathom setup` prints all ✅ (or a clear ❌ with the fix).
- [ ] Make a 2-minute fake call wav: generate English and Urdu lines with edge-tts and concatenate them with pauses. `phathom process` on it creates a call with `status='done'`, a sensible summary, and segments with timestamps.
- [ ] `phathom show` prints it correctly. A notification appears.
- [ ] Unit tests pass: `test_store.py` (uses a separate namespace `phathom_test`, cleaned up afterwards) and `test_llm_parsing.py` (mocked Groq; covers bad JSON → retry → failure path).

### Phase 3 — Auto-answer daemon (notes mode)
1. Implement `state.py`, `contacts.py`, `daemon.py` (state machine §2.3), and the CLI commands `login`, `start`, `stop`, `status`, `mode`.
2. In notes mode: answer → play disclosure → record → detect end → pipeline.

**Acceptance (🛑 test calls with the human):**
- [ ] `mode notes`: a call is answered after `ANSWER_DELAY_S`, the disclosure plays, and the call ends when the caller hangs up. A summary arrives within 60s of hang-up.
- [ ] `mode off`: the call rings normally; the bot does nothing.
- [ ] Owner answers on the phone within the delay: the bot does nothing and logs "answered elsewhere".
- [ ] Blocked contact: not answered.
- [ ] `phathom stop` mid-call: hangs up cleanly; audio modules are unloaded; no leftover Chrome or parec processes (`pgrep -f parec`).
- [ ] Logged-out profile: the daemon notifies and stays alive.

### Phase 4 — Ask
1. Implement `llm.answer_question`, retrieval (§9.11), and `phathom ask`.

**Acceptance:**
- [ ] With at least 3 processed calls (real or fake), these return correct, cited answers: "what did <caller> want?", "any action items for me this week?", and a question with no answer (it must say it doesn't know).

### Phase 5 — Talking assistant
1. Implement `vad.py` (+ `test_vad.py` on fixture wavs: expected utterance counts ±1), `tts.py` (cache, fallback), `llm.live_reply`, `agent.py`, and the `brief` command. Pre-synthesise the fixed lines when the daemon starts.
2. Wire `mode assistant` into the daemon.

**Acceptance (🛑 test calls):**
- [ ] English call: greeting → caller states purpose → bot replies sensibly → takes a message → says goodbye → hangs up. The `message_for_owner` in the DB matches.
- [ ] Urdu call: the bot replies in Urdu.
- [ ] Briefing test: set `brief "If Ahmed asks about the report, say it will be sent tonight."`. When the caller asks, the bot says that, and doesn't invent anything else.
- [ ] Barge-in: talking over the bot stops its playback within ~0.5s.
- [ ] Asking "are you a human?" gets an honest answer.
- [ ] Median turn latency logged < 3s over the test calls.
- [ ] Kill the network for 10s mid-call: the bot plays the fallback/goodbye and the daemon survives.

### Phase 6 — Polish
1. systemd **user** units: `phathom-db.service` and `phathom.service` (`After=phathom-db graphical-session.target`, `Restart=on-failure`). Document `systemctl --user enable --now phathom`.
2. Audio retention cleanup (§9.10 step 8).
3. A `README.md` with setup, daily use, and troubleshooting (selectors broke → rerun `scripts/discover.py`).

**Acceptance:**
- [ ] After reboot and login, the daemon runs and answers a test call without manual steps.

---

## 12. Failure handling (must implement)

| Situation | Behaviour |
|---|---|
| Groq 429 / 5xx | Retry per §9.7; live turn falls back to the fixed line; pipeline marks `failed` and keeps audio |
| Groq key invalid | `setup` and daemon start fail fast with a clear message |
| edge-tts fails | Groq TTS for English; Urdu → English fallback line |
| SurrealDB down | Daemon still answers and records; pipeline retries the DB 3× over 30s, then writes `data/recordings/<id>/result.json` and notifies. `phathom process --from-json` imports it later. |
| Selector not found / UI changed | `debug_dump`, notify "WhatsApp UI changed — rerun discovery", stay idle (never click blindly) |
| Chrome crashes | Restart the browser once; if it fails again within 5 min, notify and exit non-zero (systemd restarts it) |
| Audio routing fails (silent caller track for 15s while the call is active) | Call `route_chrome()` once; if still silent, log, notify, and switch to notes behaviour for this call |
| Two calls at once | Answer only the first; ignore others |
| Disk < 1 GB free | Notify; don't start new recordings; let calls ring |

---

## 12b. Copilot mode — owner on the call himself (added after Phase 6)

The owner answers (within `ANSWER_DELAY_S`) or places a call himself **in the Phathom WhatsApp window**. Calls taken on the phone can't be captured.

- **Detection:** a connected call ("End call" visible) that the bot didn't accept. Checked both when a ring disappears during the answer delay and on every idle poll (outgoing calls).
- **Audio:** Chrome is always wired to the virtual devices, so for owner calls `AudioDevices.enable_passthrough()` loads two loopbacks for the duration of the call: `phathom_speaker.monitor → default sink` (he hears the caller) and `default source → phathom_voice` (the caller hears him). They're unloaded after the call, so his mic never leaks into bot-handled calls. Headphones are recommended to avoid echo.
- **Recording:** state flag `copilot` (default true; `phathom copilot on|off`). On: record `caller.wav` and `owner.wav` (from `phathom_voice.monitor`), call `mode='copilot'`, speakers `caller` / `owner`. Off: passthrough only, nothing stored.
- **Never** hang up an owner call, including on shutdown.
- **Call end:** "End call" gone, **or** `CALL_TIMER` text unchanged for 4s (the call screen can linger after hang-up).
- Copilot works in every mode, including `off`. Disconnecting (stopping the daemon) closes the browser, so nothing is answered or recorded.

---

## 13. Out of scope for v1 (don't build)

- Group calls, video (the camera always stays off), screen share.
- Voice cloning or pretending to be the owner.
- Making outgoing calls.
- Reading or sending WhatsApp text messages.
- A web UI (CLI only; maybe v2).
- Local STT/LLM/TTS models.

---

## 14. Definition of done

All phase acceptance boxes are ticked. `pytest` is green. `README.md` exists. The human has confirmed one real end-to-end call in each mode. The final report lists: the WhatsApp Web build used for the selectors, the measured median latency, any spec deviations (with reasons), and known issues.
