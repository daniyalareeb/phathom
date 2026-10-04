# MyPhathom v2 — Chrome Extension + Local Server (Implementation Spec)

> Audience: the coding model implementing this. Read `SPEC.md` first: it describes the existing
> Python code (store, pipeline, agent, AI clients, prompts), which this spec **reuses**.
> This document replaces only the *browser/audio layer* (Playwright + PulseAudio) and adds a UI.

> **BUILD-FIRST MODE (current).** The user has decided: build everything in §12 in one go,
> **without stopping at checkpoints**. Real-call testing happens once at the end (§12, "Final test
> checklist"), and a reviewer fixes the bugs found. Where something is unproven, build **all** the
> documented options behind a setting with an automatic choice, so the final test can pick the winner
> without a rebuild.

### Current status (2026-10-03)

| Area | Status |
|---|---|
| Phase 0 spike | Built (`extension/`, `phathom/serve_spike.py`, `phathom serve --spike`). Proven on real calls: the call UI is in the top-level tab; ring/answer/end + incoming/outgoing detection work; the owner mic tap works (real speech in `owner.wav`); the AudioWorklet loads under WhatsApp's CSP. |
| Caller audio | **Unsolved.** WhatsApp's RTCPeerConnection has **no media receivers** (`pc_receivers: []`). Audio is decoded in WhatsApp's own worker and played through its own AudioWorklets (`static.whatsapp.net` modules) into a 16 kHz AudioContext. The first speaker tap only caught the ringback. Per-node taps with live RMS (Step 1A) are built but not yet tested. See §5.7. |
| Bot voice into the call | Unknown (the test-tone result hasn't been reported). Build Talk mode anyway, behind a setting (§5.3a). |
| Outgoing peer name | Shows "Unknown": the name selector doesn't match on the outgoing call screen. |
| Phase 1 server | **Done** (`phathom/server/*`, `phathom serve`). Token auth, REST, WS, `decide()`, CallSession, WsPlayer, WavSink, live transcript, reconnect grace. 63 pytest + 6 vitest pass. |
| inject.js constructor hooks | Must use `wrapCtor()` (Reflect.construct + new.target), never `new Orig()`, or WhatsApp's subclasses (`class X extends AudioWorkletNode`) break. |

---

## 0. Rules (same discipline as SPEC.md §0)

1. Build the §12 steps **in order, without stopping** for real-call tests. Don't wait for the user
   between steps. Unit tests (pytest + vitest) must be green at the end of each step.
2. Real-call verification happens **once, at the end**, using the §12 Final test checklist and the
   Diagnostics page (§7.5). Make that test easy: every unproven part must report what it did in the
   Diagnostics page and the server log.
3. **Never invent WhatsApp selectors.** Reuse the verified ones in `phathom/whatsapp/selectors.py`, or
   derive new ones from the real DOM dumps in `data/dumps/*.html`. If a needed element is not in any
   dump, put a best-effort selector in `extension/src/content/selectors.ts` marked
   `// UNVERIFIED: needs DOM dump`. Make the feature fail **visibly** (a Diagnostics row + a log line),
   never silently, and list it in the final report.
4. Items marked ⚠️ are unverified assumptions. Don't block on them: build the primary path **and** the
   documented fallback, choose between them at runtime (setting with an `auto` default), and expose the
   choice in Diagnostics.
5. **Reuse, don't rewrite**, the Python modules: `store.py`, `pipeline.py`, `agent.py`, `ai/*`,
   `contacts.py`, `state.py`, `config.py`. Change them only where this spec says so.
6. No local models. All AI goes through Groq (STT/LLM) and edge-tts (TTS), as today.
7. Keep the old Playwright/PulseAudio path working behind `phathom start --legacy` until Phase 7.
8. Every new Python module gets pytest tests. The extension gets a small manual test checklist per phase.
9. Don't add features that aren't in this spec. Ask instead.

---

## 1. Why change the architecture

The current build drives a *separate* Chrome through Playwright and routes call audio through
PulseAudio virtual devices. That's the most fragile part, and it's invisible (no UI).

The new design moves the "eyes, ears and hands" into a Chrome extension that runs inside the user's
**own** WhatsApp Web tab:

| Concern | v1 (Playwright + Pulse) | v2 (Extension) |
|---|---|---|
| Browser | Second, automated Chrome profile | User's normal Chrome, normal WhatsApp tab |
| Detecting calls | Polling selectors every 500 ms | `MutationObserver`, instant |
| Caller audio | Null sink + `parec` | WebRTC remote track tapped directly |
| Owner audio (Copilot) | Loopback modules | Mic stream tapped directly |
| Who said what | Inferred | **Exact**: separate caller/owner/bot tracks |
| Bot voice | Virtual mic via remap-source | Mixed into the mic stream WhatsApp sends |
| "Take over" mid-call | Not possible | Mute bot, unmute your mic, one click |
| UI | CLI only | Popup, side panel, full dashboard |
| Linux audio setup | Required | None |

The Python side stays the "brain": Groq, SurrealDB, the agent loop, summaries and Q&A. It becomes a
local server the extension talks to.

---

## 2. Architecture

```
┌──────────────────────── Chrome (user's normal profile) ─────────────────────────┐
│                                                                                  │
│  web.whatsapp.com tab                                                            │
│  ┌───────────────────────────┐   window.postMessage   ┌──────────────────────┐   │
│  │ inject.js  (MAIN world)   │ ◄────────────────────► │ content.js (ISOLATED)│   │
│  │ • wraps getUserMedia      │   PCM frames, control  │ • MutationObserver   │   │
│  │ • wraps RTCPeerConnection │                        │ • ring/accept/hangup │   │
│  │ • audio mixer + taps      │                        │ • chat reader (Brain)│   │
│  └───────────────────────────┘                        └─────────┬────────────┘   │
│                                                     chrome.runtime Port          │
│  ┌──────────────────────────────────────────────────────────────▼────────────┐   │
│  │ background.js (service worker): connection state, WS client, badge,       │   │
│  │ notifications, routing between tab ⇄ server ⇄ UI pages                    │   │
│  └──────────────▲───────────────────────────────▲────────────────────────────┘   │
│                 │ runtime messages              │                                │
│   popup.html (quick controls)   sidepanel.html (live call)   app.html (dashboard)│
└─────────────────┼───────────────────────────────┼────────────────────────────────┘
                  │  WebSocket ws://127.0.0.1:8765/ws  +  REST http://127.0.0.1:8765/api
┌─────────────────▼────────────────────────────────────────────────────────────────┐
│ phathom serve  (FastAPI + uvicorn, Python)                                        │
│  call_session.py  (state machine, decides accept/ignore, runs recorder/agent)     │
│  agent.py (unchanged logic, new WsPlayer)   pipeline.py   store.py   ai/*          │
│  brain.py (chat import + per-contact knowledge)                                   │
└──────────────────────────────────────┬───────────────────────────────────────────┘
                                       │
                           SurrealDB 3.x  ws://127.0.0.1:8000/rpc
```

### 2.1 Responsibilities (strict split)

- **Extension decides nothing about policy.** It reports events (`ring`, `call_started`, `call_ended`)
  and executes commands (`accept`, `hangup`, `play`, `stop_audio`, `set_mic_gain`).
- **Server owns all policy**: modes, contacts allow/block, answer delay, when to hang up, what to say.
  This keeps the logic in Python where it's testable.
- **Safe default:** if the server is unreachable, the extension **never** auto-answers. Badge turns red.

---

## 3. Modes (what the user sees)

The UI exposes one master switch and two independent options:

| Control | Values | Meaning |
|---|---|---|
| **Connect** | on / off | Off = Phathom does nothing at all: no answering, no recording. |
| **Answer for me** | off / listen / talk | Incoming calls you don't pick up within `answer_delay_s` get answered by Phathom. `listen` = stays silent after the disclosure line, takes notes. `talk` = full assistant conversation. |
| **Copilot** | on / off | Calls **you** answer or **you** make are recorded with both sides, transcribed and summarized. Phathom never speaks. |

Mapping to the existing `state.json` keys (keep them, for CLI compatibility):
`mode = off | notes | assistant` ↔ Answer-for-me `off | listen | talk`; `copilot` unchanged; add
`connected: bool` (Connect switch).

Decision table, evaluated by the server (`call_session.decide()`):

| Event | Connected | Answer-for-me | Copilot | Contact rule | Action |
|---|---|---|---|---|---|
| incoming ring | off | any | any | any | ignore |
| incoming ring | on | off | any | any | ignore (if you answer, Copilot rules apply) |
| incoming ring | on | listen/talk | any | `block` | ignore |
| incoming ring | on | listen/talk | any | allow/default | wait `answer_delay_s`; if still ringing → `accept`, run bot in that mode |
| you answered (ring vanished, call active) | on | any | on | not `block` | record as `copilot` |
| you placed a call (outgoing) | on | any | on | not `block` | record as `copilot` |
| any call | on | any | off, and not bot-answered | any | do nothing |

Contacts keep the existing `contacts.yaml` semantics (SPEC §6), editable from the UI.

---

## 4. Repository layout (additions)

```
MyPhathom/
├── extension/                     # NEW — Chrome MV3 extension (TypeScript, built with Vite)
│   ├── package.json
│   ├── vite.config.ts             # multi-entry build → extension/dist
│   ├── public/manifest.json
│   ├── public/icons/              # 16/32/48/128 px, idle/connected/live/error variants
│   ├── public/worklets/pcm-tap.js # AudioWorklet (plain JS, web-accessible)
│   ├── src/inject/                # MAIN-world: media hooks, mixer, taps
│   ├── src/content/               # ISOLATED-world: DOM watcher, actions, chat reader
│   │   └── selectors.ts           # ported 1:1 from phathom/whatsapp/selectors.py
│   ├── src/background/            # service worker
│   ├── src/shared/                # protocol types, api client, constants
│   └── src/ui/                    # React: popup, sidepanel, app (dashboard), components
├── phathom/server/                # NEW
│   ├── app.py                     # FastAPI app factory, auth, CORS
│   ├── api.py                     # REST routes
│   ├── ws.py                      # WebSocket endpoint + protocol
│   ├── call_session.py            # state machine + decide()
│   ├── ws_audio.py                # WavSink, WsPlayer
│   └── events.py                  # pub/sub for UI live updates
├── phathom/brain.py               # NEW — chat import, extraction, brain building
└── tests/server/ , tests/brain/   # NEW
```

---

## 5. Extension

### 5.1 Tech

- Manifest V3, TypeScript, **Vite** multi-page build. UI in **React 18 + Tailwind CSS**.
  Hand-written components in the shadcn/ui style (no runtime CDN; MV3 forbids remote code).
- Icons: `lucide-react`. Font: **Inter** variable, bundled as woff2 in `public/fonts/`.
- No other runtime dependencies without asking. Bundle size target for UI: < 400 KB gzipped.

### 5.2 manifest.json (shape)

```json
{
  "manifest_version": 3,
  "name": "MyPhathom",
  "version": "0.2.0",
  "minimum_chrome_version": "116",
  "permissions": ["storage", "sidePanel", "notifications", "alarms", "tabs"],
  "host_permissions": ["https://web.whatsapp.com/*", "http://127.0.0.1:8765/*"],
  "background": { "service_worker": "background.js", "type": "module" },
  "action": { "default_popup": "popup.html" },
  "side_panel": { "default_path": "sidepanel.html" },
  "content_scripts": [
    { "matches": ["https://web.whatsapp.com/*"], "js": ["inject.js"],
      "world": "MAIN", "run_at": "document_start", "all_frames": true },
    { "matches": ["https://web.whatsapp.com/*"], "js": ["content.js"],
      "run_at": "document_idle", "all_frames": true }
  ],
  "web_accessible_resources": [
    { "resources": ["worklets/pcm-tap.js"], "matches": ["https://web.whatsapp.com/*"] }
  ],
  "options_page": "app.html#/settings"
}
```

`inject.js` must run at `document_start` in the MAIN world so the hooks are installed **before**
WhatsApp's code captures references to `getUserMedia` / `RTCPeerConnection`.

### 5.3 inject.js (MAIN world): media hooks

Install once (guard with `window.__phathom`). Never throw into WhatsApp's code: wrap every hook body
in try/catch and fall back to the original behaviour.

**a) Mic mixer: wrap `navigator.mediaDevices.getUserMedia`.**
When a request includes `audio`:

1. Call the original to get the real mic stream `realMic`.
2. Build (one shared `AudioContext`, `sampleRate: 48000`):
   ```
   realMic ─► micGain ─┬─► mixDest (MediaStreamAudioDestinationNode)
                       └─► ownerTap  (pcm-tap, track="owner")
   ttsSource(s) ─► botGain ─┬─► mixDest
                            └─► (bot audio is also saved server-side; no tap needed)
   ```
3. Return a new `MediaStream` = `mixDest`'s audio track + any **video** tracks from the original
   (video calls keep the real camera untouched).
4. Forward `stop()` on the returned audio track to the real mic tracks.
5. Default gains: `micGain = 1`, `botGain = 1`. During a bot-answered call: `micGain = 0` (the owner's
   room noise must not leak) until the user presses **Take over**.

⚠️ Verify in Phase 0 that WhatsApp accepts the substituted track (the caller hears bot audio).
Fallback if WhatsApp rejects it: assistant **talk** mode is unavailable in v2. Keep `listen` and Copilot,
and say so in the UI. Do **not** fall back to PulseAudio inside the extension path.

**b) Remote audio tap: wrap `window.RTCPeerConnection`.**
Subclass it (keep `prototype`, static methods and `name`). On every `track` event with
`track.kind === "audio"`, connect `new MediaStream([track])` → `MediaStreamAudioSourceNode` →
`pcm-tap` (track="caller").

⚠️ Known Chromium behaviour: a remote WebRTC stream can produce silence in Web Audio unless it is also
attached to a media element. If Phase 0 shows silent caller frames, also attach it to a muted, detached
`new Audio()` with `srcObject` set and `play()` called.

If there is more than one remote audio track (group calls), tag them `caller:1`, `caller:2`, … in
order of arrival. Group calls are **out of scope** for v2 but must not crash anything. Record only
`caller:1` and log the rest.

**c) pcm-tap AudioWorklet** (`public/worklets/pcm-tap.js`): downmix to mono, resample 48 kHz →
16 kHz (simple FIR/linear is fine), convert to s16le, emit **30 ms frames (960 bytes)**, batched as
100 ms chunks (3200 bytes) via `port.postMessage` with transfer. Load it with
`audioContext.audioWorklet.addModule(chrome-extension URL passed in from content.js)`.
⚠️ If WhatsApp's CSP blocks the worklet, fall back to a `ScriptProcessorNode` implementation with
identical output. Both must pass the same unit test (sine in → expected 16 kHz frames out).

**d) Bot playback.** Receive PCM s16le mono 24 kHz chunks for an utterance id. Schedule them
gap-free with `AudioBufferSourceNode`s on `botGain`. Support `stop_audio` (stop all scheduled
sources immediately, for barge-in) and report `played {id, completed: bool}` when the last buffer ends
or is stopped.

**e) Control surface.** inject.js talks **only** to content.js through `window.postMessage` with
`{source: "phathom", ...}` envelopes, and ignores any message without a per-page random nonce that
content.js generates and hands over at startup (stops the page or other scripts spoofing commands).

### 5.7 Caller audio: pluggable sources (replaces the single-path design in §5.3b)

It's unproven which path carries the caller's voice, so build all three and select at runtime.
Setting `caller_source`: `auto` (default) | `webrtc` | `speaker_tap` | `tab_capture`.

1. **`webrtc`**: the §5.3b RTCPeerConnection track tap. Keep it, though real calls showed no
   receivers.
2. **`speaker_tap`**: the AudioNode.connect hook taps every non-ours node that connects to any
   AudioDestinationNode, one tap per node (`speaker:N`). **Auto-pick:** for each tap, keep a rolling
   5 s count of VAD-voiced frames (webrtcvad, server-side) and of frames voiced in the owner track at
   the same time. The caller tap is the one with the most voiced frames that are **not** simultaneous
   with owner speech (this rejects echoes of the mic and silent keep-alive nodes). Re-evaluate every
   5 s during the call. Feed the chosen tap to the session as `caller`. At call end, write all taps to
   disk and run the same choice over the whole call, so the pipeline uses the best tap even if the live
   choice was wrong.
3. **`tab_capture`**: `chrome.tabCapture.getMediaStreamId({targetTabId})`, obtained on the user's
   **Connect** click in the popup (a user gesture), opened in an **offscreen document**
   (`chrome.offscreen`, reason `USER_MEDIA`). The offscreen page plays the captured stream back to the
   speakers (capture mutes the tab otherwise), runs the same pcm-tap at 16 kHz, and sends frames as
   track `caller` only between `call_started` and `call_ended`. The capture includes everything the tab
   plays (it can't include the owner's own voice, which isn't played locally).
   ⚠️ If Chrome needs a fresh gesture (e.g. after a browser restart or a tab reload), show the popup
   card "Click Connect once on the WhatsApp tab to enable call audio" and fall back to `speaker_tap`.

**`auto` order:** `webrtc` if a remote audio track appears. Otherwise `tab_capture` if a capture is
active. Otherwise `speaker_tap` with auto-pick. Diagnostics shows the active source, every tap with a
live level meter, and which one auto-pick chose.

Server: add binary kind `0x04` = `tab` (tab-capture audio) and JSON audio messages with a `track`
label for `speaker:N` (already supported by the spike). `CallSession` maps the chosen source to the
`caller` track. Keep the raw per-tap wavs for 7 days under `data/recordings/<call_id>/taps/` for
debugging.

### 5.4 content.js (ISOLATED world)

**a) Call watcher.** A `MutationObserver` on `document.body` (subtree, childList, attributes),
debounced to ~100 ms, plus a 1 s safety poll. Use the selectors ported from `selectors.py`
(`ACCEPT_BUTTON`, `HANGUP_BUTTON`, `INCOMING_CALLER`, `CALL_TIMER`, `LOGGED_IN_MARKER`, …).
Reproduce the call-end logic already implemented in `phathom/whatsapp/driver.py`
(`is_call_active`: End-call button visible; timer present and unchanged for `TIMER_STALE_S = 4` s ⇒ ended).
Emit to background:
- `wa_status {logged_in: bool}` on change.
- `ring {caller}` when the incoming-call UI appears, and `ring_gone` when it disappears.
- `call_started {direction: "incoming"|"outgoing", peer, answered_by: "owner"|"bot"}`.
  `outgoing` = call became active with no preceding ring. Read the peer name from the call UI
  (`current_peer_name()` logic).
- `call_ended {reason}`.

⚠️ Phase 0 must establish **where** the call UI lives: same tab, an iframe, or a popup window. If it's
a separate window, `all_frames` + the `matches` pattern must still cover it. Report this at the 🛑.

**b) Actions** (commands from background): `accept`, `hangup` (with the same verify-and-retry rule as
`driver.hang_up()`), `open_chat {name}` (Brain/task calls, Phase 5–6).

**c) Audio relay.** Forward inject.js PCM chunks to background over a long-lived
`chrome.runtime.connect({name: "audio"})` Port. Encode as base64 strings (runtime messaging is JSON).
Forward bot audio/commands from background to inject.js.

**d) Liveness.** Only one WhatsApp tab may be "the active tab". If two are open, background picks the
most recently focused one and tells the others to stay idle.

### 5.5 background.js (service worker)

- Holds the single WebSocket to the server (`ws://127.0.0.1:8765/ws?token=…`). An open WebSocket
  with traffic keeps an MV3 worker alive (Chrome ≥ 116). Also send an application `ping` every 20 s.
- Reconnects with backoff (1, 2, 5, 10, 30 s).
- Relays: tab ⇄ server (events, PCM, commands) and server → UI pages (live events).
- Badge: grey dot = disconnected, green = connected & idle, red pulsing = in a call (recording),
  amber `!` = error (server down, WhatsApp logged out, Groq key missing).
- Notifications: "Phathom answered Ahmed", "Summary ready — Ahmed (4 min)". Clicking opens the
  dashboard on that call.
- **Connect** from the UI: tell the server `connected=true`; if no WhatsApp tab exists, open one
  (pinned). **Disconnect**: tell the server `connected=false`; if a bot-answered call is live, the
  server ends it gracefully. Copilot recording of the owner's own call stops (the call itself continues).
- `chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: false})`. Auto-open the side panel on
  `call_started` only if the user enabled it in Settings (default on).

### 5.6 Auth and safety between extension and server

Any website could talk to a localhost server, so:

- On first `phathom serve`, generate a random 32-byte token in `data/server_token` (mode 600).
- The extension's first-run screen asks the user to paste it once (`phathom token` prints it).
  Stored in `chrome.storage.local`.
- Every REST call sends `Authorization: Bearer <token>`. The WS sends `?token=`.
- The server rejects any request with a wrong token, and any `Origin` other than
  `chrome-extension://<id>` (the id is configurable: `EXTENSION_ID` in `.env`, also learned from the
  first valid connection and pinned afterwards).
- Bind to `127.0.0.1` only. Never `0.0.0.0`.

---

## 6. Server (`phathom serve`)

New CLI command: `phathom serve [--port 8765]`, FastAPI + uvicorn, one process. It connects to
SurrealDB at startup (same `Store`), and replaces the daemon when the extension is used. `phathom start`
keeps working as the legacy path. Add deps: `fastapi`, `uvicorn[standard]`, `python-multipart`.

### 6.1 WebSocket protocol (`/ws`)

Text frames are JSON `{"t": "<type>", ...}`. Binary frames carry audio:
`[1 byte kind][4 bytes uint32 LE seq][PCM s16le mono]`.

| Kind byte | Direction | Content |
|---|---|---|
| `0x01` | ext → server | caller audio, 16 kHz |
| `0x02` | ext → server | owner (mic) audio, 16 kHz |
| `0x11` | server → ext | bot speech, 24 kHz; preceded by a `play_begin` JSON with the utterance id |

(The background worker converts the base64 Port messages to binary WS frames.)

JSON messages:

```
ext → server:  hello {ext_version, tab_id}
               wa_status {logged_in}
               ring {caller}            ring_gone {}
               call_started {direction, peer, answered_by}
               call_ended {reason}
               played {id, completed}
               ui_action {action: "hangup"|"take_over"|"hand_back"|"note", text?}
               pong {}
server → ext:  welcome {state}          state {connected, mode, copilot, ...}
               accept {}                hangup {}
               play_begin {id}          play_end {id}        stop_audio {}
               set_gain {mic: 0..1, bot: 0..1}
               live {call_id, event}    # forwarded to UI pages: transcript, status, summary_ready
               ping {}
```

### 6.2 call_session.py

One `CallSession` per call. Its pieces:

- `decide(event, state, contacts) -> Action`, a **pure function** implementing the §3 table, with full
  unit tests.
- Ring handling: on `ring`, if the decision is "answer", start a timer for `answer_delay_s`. If
  `ring_gone` arrives first and then `call_started{answered_by:"owner"}`, it's the owner's call (Copilot
  rules). If the timer fires while still ringing, send `accept`. Content.js then reports
  `call_started{answered_by:"bot"}`.
- Recording: a `WavSink` per track writes `data/recordings/<call_id>/caller.wav`, `owner.wav`, and
  `bot.wav` (bot audio is written server-side as it is sent, time-aligned by wall clock with silence
  padding). Track kinds map to the existing segment speakers: `caller`, `owner`, `phathom`.
- Frames also go into the `asyncio.Queue`s the existing code expects (`FRAME_BYTES = 960`).
- **talk** mode: construct `LiveAgent` exactly as the daemon does today, but pass a `WsPlayer`
  instead of `Player`. **listen** mode: play the disclosure line once, then record only.
  **copilot**: record only, never send audio.
- On `call_ended` (or a hang-up that the server ordered): stop the sinks, then run
  `pipeline.run_pipeline(...)` with `owner_wav` and `ended_at` (already supported). Publish
  `summary_ready`.
- Max call length, silence hang-up and disclosure: same settings and behaviour as today
  (`MAX_CALL_S`, `SILENCE_HANGUP_S`, `DISCLOSURE`). Copilot calls use `OWNER_CALL_MAX_S`.
- If the WS drops during a call: keep the session open for 60 s waiting for reconnect. Audio that
  arrives after reconnect is appended (gap filled with silence). After 60 s, finalize with what exists.

### 6.3 ws_audio.py

- `WavSink(path)`: `write(pcm)`, `pad_to(seconds)`, `close()` (valid header).
- `WsPlayer`: same interface as `audio/player.py:Player` (`play(wav_path) -> bool`, `interrupt()`,
  `playing`). `play` reads the wav, resamples to 24 kHz mono s16le, sends `play_begin`, the binary
  chunks (100 ms each, paced at ~1.5× real time so `stop_audio` stays responsive) and `play_end`, then
  awaits `played{id}`. Returns `completed`. `interrupt()` sends `stop_audio` and resolves the pending
  wait with `False`. `agent.py` must need **no logic changes**: only accept any object with this interface
  (add a `Protocol` type).

### 6.4 Live transcript (UI only)

During any recorded call, run the existing `vad.Segmenter` on the caller and owner queues. For each
utterance, call `transcribe_pcm` with `STT_MODEL_LIVE` and publish a `live` `transcript` event
`{speaker, text, t}`. This is display-only. The final transcript still comes from `pipeline`.
Setting `live_transcript` (default on) disables it to save Groq quota. On a 429, pause live
transcription for 60 s and show "Live transcript paused (rate limit)" in the side panel.

### 6.5 REST API (`/api`, all bearer-authenticated, JSON)

| Method & path | Purpose |
|---|---|
| `GET /health` | `{server, db, groq_key, wa_logged_in, ext_connected, version}` |
| `GET /state` · `PATCH /state` | connected, mode, copilot, answer_delay_s, live_transcript, side_panel_auto |
| `GET /calls?q=&mode=&contact=&from=&to=&cursor=` | paginated list (newest first) with title, peer, mode, duration, has_message |
| `GET /calls/{id}` | full call: summary, action items, message_for_owner, segments, audio URLs |
| `GET /calls/{id}/audio/{track}` | wav stream (caller/owner/bot/mix) with Range support |
| `PATCH /calls/{id}` | title, starred, notes |
| `DELETE /calls/{id}` | delete call + segments + recordings |
| `POST /calls/{id}/reprocess` | re-run pipeline |
| `POST /ask` | `{question}` → `{answer, citations:[{call_id, t}]}` (wraps existing ask logic) |
| `GET /contacts` · `PUT /contacts` | read/write `contacts.yaml` (validate; keep comments if feasible, else keep a `.bak`) |
| `GET /brief` · `PUT /brief` | today's instructions for the bot (`brief.md`) |
| `GET /settings` · `PATCH /settings` | whitelisted `.env`-backed settings (voices, max call, disclosure); **never** expose or return `GROQ_API_KEY`, only `groq_key: "set"|"missing"` + a write-only setter |
| `GET /stats` | calls this week, minutes recorded, open action items |
| `GET /action-items?open=true` · `PATCH /action-items/{id}` | cross-call to-do list |
| Brain routes | see §8 |

### 6.6 Fixes to carry over now

- `LLM_MODEL_SMART` (`qwen/qwen3.8-27b`) hit Groq's free-tier **output-tokens-per-minute limit
  (1000)** in the logs: `Requested 1015`. Cap summary `max_tokens` at **900** and keep the existing
  fallback to `LLM_MODEL_LIVE` on 429. Make both values settings.

---

## 7. UI

### 7.1 Design language ("premium, calm, professional")

- Dark-first with a full light theme (follows the system; manual override in Settings).
- Neutral graphite surfaces, **one** accent: emerald for "connected/live". Red only for recording
  indicators and destructive actions. Amber for warnings.
- Design tokens in one `tokens.css` (CSS variables), consumed by Tailwind config. No hard-coded colors
  in components.
- Type: Inter. Scale 12/13/14/16/20/28. Numbers use `font-variant-numeric: tabular-nums`.
- 8 px spacing grid, 12 px radii on cards, 1 px hairline borders, subtle shadows only on popovers.
- Motion: 150–200 ms ease-out. The live indicator pulses. Respect `prefers-reduced-motion`.
- Every list has **empty**, **loading** (skeletons, not spinners) and **error** (with Retry) states.
- Keyboard: `⌘/Ctrl K` command palette in the dashboard (jump to call, ask, toggle connect).
  Visible focus rings. All controls have labels (a11y).
- Copy is short and human: "Phathom will answer calls you miss after 6 s", not "auto_answer=true".

### 7.2 Popup (360 × ~480): the quick switch

```
┌─────────────────────────────────────┐
│ ◉ MyPhathom                  [⚙]   │
│                                    │
│   ═════════  CONNECTED  ●═════════   │  ← large toggle, the main action
│   Ready · WhatsApp linked          │
│                                    │
│ Answer for me                      │
│ [ Off ][ Listen ][ Talk ]          │  ← segmented control
│ After 6 s if you don't pick up     │
│                                    │
│ Copilot on my calls          [● ]  │
│                                    │
│ ── Status ──                       │
│ ✓ Server   ✓ Database  ✓ Groq      │
│                                    │
│ Last: Ahmed · 4 min · 2 h ago   ›  │
│ [ Open dashboard ]  [ Side panel ] │
└─────────────────────────────────────┘
```

If the server is unreachable, the whole popup becomes one clear card: "Server isn't running" with the
command `phathom serve` and a Copy button. If the token isn't set yet: the first-run card.

### 7.3 Side panel: live call companion

- **Idle:** connection state, today's brief (editable inline), next steps ("Call yourself to test").
- **Ringing:** caller name, contact rule badge, Brain card for that contact (§8), countdown
  "Phathom answers in 4…" with **Let it ring** (ignore this call) and **Answer now (bot)**.
- **In call:** header with peer, mode chip (Talk / Listen / Copilot), timer, red REC dot.
  - Live transcript, with speakers color-coded: You / Caller / Phathom. Auto-scrolls; pauses when
    the user scrolls up.
  - Talk mode buttons: **Take over** (bot muted, mic on; agent stops speaking and listening for
    replies but recording continues), **Hand back**, **Hang up**.
  - **Note** input: the user types a private note, saved as a timestamped `owner_note` segment and
    included in the summary.
- **After call:** "Summarizing…" skeleton, then the summary card, with a link to open in the dashboard.

### 7.4 Dashboard (`app.html`, full tab): left nav

```
┌──────────────┬────────────────────────────────────────────────────┐
│ ◉ MyPhathom  │  [● Connected]  Answer: Talk  Copilot: On         ⌘K Search │
│              ├────────────────────────────────────────────────────┤
│  Home        │                                                        │
│  Calls       │                (page content)                          │
│  Ask         │                                                        │
│  Brain       │                                                        │
│  To-dos      │                                                        │
│  Contacts    │                                                        │
│  Settings    │                                                        │
└──────────────┴────────────────────────────────────────────────────┘
```

The top bar is always visible, so connection and modes are one click from every page. Hash routing
(`#/calls/<id>`) so links from notifications work.

| Page | Content |
|---|---|
| **Home** | Live call card (if any). "Messages for you" from bot-answered calls (unread first). Open to-dos. This week's stats. Last 5 calls. |
| **Calls** | Filter bar (search, mode, contact, date range) + list grouped by day: avatar initial, name, mode chip, duration, one-line gist, message dot. **Call detail**: title (editable), meta row, *Summary*, *Message for you* (highlighted), *Action items* (checkboxes → To-dos), *Transcript* (speaker-colored, click a line to seek), audio player with a track switch (Mix / Caller / You / Phathom), Reprocess, Delete (confirm). |
| **Ask** | Chat-style Q&A over calls (and Brain, after Phase 5). Answers show citation chips that open the call at that timestamp. Suggested questions. |
| **Brain** | §8. |
| **To-dos** | All action items across calls, filter open/done, grouped by person; check off. |
| **Contacts** | Table: name, rule (Allow / Block / Default), language, notes for the bot. Add/edit in a drawer. Saves to `contacts.yaml`. |
| **Settings** | General (answer delay, side panel auto-open, theme), Voice (EN/UR voices with a Preview button), Calls (max length, silence hang-up, disclosure, live transcript), Privacy (keep audio N days, delete all data), Connection (server URL, token, test button), Groq key (write-only). |

### 7.5 Diagnostics (Settings → Diagnostics, also a dev section in the side panel)

Built for the final test, and kept afterwards for troubleshooting:

- Hook status (getUserMedia, RTCPeerConnection, AudioNode.connect, worklet vs ScriptProcessor).
- WhatsApp status: logged in, active tab, and the selector check (each selector in `selectors.ts`:
  found / not found on the current screen; UNVERIFIED ones are flagged).
- Caller source: the current mode and the auto-pick result, plus a live level meter per tap
  (`speaker:N`, `tab`, `webrtc`) and for the owner mic.
- Buttons: **Play test tone** (through the bot path), **Dump DOM** (labelled), **Copy diagnostics**
  (a JSON blob with everything above plus the last 200 log lines, for the reviewer).

---

## 8. Brain (Phase 5): knowledge from chats

Goal: pick a contact or group, and Phathom builds a short, current "brain" about it from the chat
history, used by Ask, shown on the side panel when they call, and given to the bot as context.

### 8.1 Two ways to get messages

1. **Import (most reliable, do first):** WhatsApp → chat → *Export chat* (without media) →
   upload the `.txt`/`.zip` in Brain → Import. Parse both Android (`dd/mm/yyyy, hh:mm - Name: text`) and
   iOS (`[dd/mm/yyyy, hh:mm:ss] Name: text`) formats, 12/24 h, multi-line messages, system lines.
   Write parser tests with fixtures for each format.
2. **Read from WhatsApp Web (convenience):** with a chat open, side panel → **Read this chat**
   → choose a range (last 7 / 30 / 90 days, or "since last read"). content.js scrolls the message pane
   up in small steps (≥ 800 ms apart, stop at the range or at 5,000 messages), then extracts
   `{sender, timestamp, text, is_me}` from the bubbles. Selectors for bubbles and timestamps come from
   a DOM dump (§11.4), never guessed. Show progress and a Cancel button. Voice notes: list them as
   `[voice note mm:ss]`; transcription of voice notes is out of scope for v2.

### 8.2 Data model (SurrealDB, add to schema)

```
chat     { id: slug, name, kind: 'direct'|'group', last_read_at, msg_count, created_at }
message  { chat: record<chat>, ts: datetime, sender, is_me: bool, text, hash }   -- UNIQUE(chat, hash)
         FULLTEXT index on text (same analyzer as segments)
brain    { chat: record<chat>, updated_at, summary, people: array, facts: array,
           open_loops: array, decisions: array, timeline: array, last_msg_ts }
```

Dedup on `hash = sha1(ts + sender + text)` so re-reading or re-importing never duplicates.

### 8.3 Building the brain

- Chunk new messages (since `brain.last_msg_ts`) into ~12k-char windows. For each window, extract
  JSON `{facts, open_loops, decisions, people, timeline_events}` with `LLM_MODEL_LIVE` (cheap). Then
  merge into the existing brain with `LLM_MODEL_SMART` (dedupe, resolve contradictions with the newest
  message winning, keep each list ≤ 30 items, `summary` ≤ 120 words).
- Every fact keeps a source `{message_ts}` so the UI can show "from 12 Sep" and Ask can cite it.
- Rate limits: process sequentially, honour 429 backoff, show progress "Building brain… 3/12".

### 8.4 Uses

- **Brain page:** list of chats with last updated time; detail shows Summary, People, Open loops,
  Decisions, Key facts, Timeline, and "Ask about this chat".
- **Ask:** evidence search covers `message` + `brain` + call segments; citations distinguish
  "call" and "chat".
- **Calls:** when a contact with a brain calls, the side panel shows its Summary + Open loops, and
  `LiveAgent` gets the brain summary appended to `contact_notes` (cap 1,500 chars).

### 8.5 Brain API

`GET /brain` · `GET /brain/{chat}` · `POST /brain/import` (multipart) · `POST /brain/{chat}/messages`
(batch from the reader) · `POST /brain/{chat}/rebuild` · `DELETE /brain/{chat}`.

---

## 9. Task calls (Phase 6, optional, ask the user before starting)

The user writes a task ("Call Ahmed, ask if the report is ready; if not, get a date"). Phathom places
the call and runs `LiveAgent` with a **task prompt** (goal, allowed facts, when to stop), then reports
the outcome.

- Only contacts with rule `allow` (or a new `allow_task_calls: true` flag).
- The user confirms each call in the UI; no scheduled or bulk calling. Max 1 task call per 10 minutes.
- Disclosure is mandatory in task calls (cannot be turned off): "Hi, this is Daniyal's AI assistant
  calling on his behalf…".
- The "call" button selector must come from a DOM dump.
- Result card: outcome, answer to the task question, transcript.

---

## 10. Failure handling

| Failure | Behaviour |
|---|---|
| Server down | Badge amber, popup shows the "Server isn't running" card, **no auto-answer**. |
| WhatsApp logged out | Badge amber, popup says "Open WhatsApp Web and scan the QR code" with an Open button. |
| No WhatsApp tab | On Connect, open one (pinned). If closed while connected: notification "Phathom can't hear calls: WhatsApp tab closed" + Reopen. |
| WS drop mid-call | Extension buffers up to 5 min of PCM in memory and flushes on reconnect; server waits 60 s (§6.2). |
| Hooks failed (inject error) | content.js reports `hooks_ok:false` in `hello`; server disables talk/listen auto-answer and Copilot for that tab; UI shows why. |
| Groq 429 / down | Same fallbacks as SPEC §12; live transcript pauses; pipeline retries later (`reprocess`). |
| Selector missing | Log a DOM dump request; the UI shows "WhatsApp changed its layout; selectors need updating". |

---

## 11. Testing

1. **Python:** pytest for `decide()`, `CallSession` with a fake WS client (scripted events + PCM from
   `tests/fixtures/*.wav`), `WsPlayer` interrupt, `WavSink` alignment, export parsers, brain merge
   (mock LLM), API auth (wrong token / wrong Origin → 401/403).
2. **Extension unit:** Vitest for the resampler/framer, protocol encoding, selector helpers (on saved
   DOM fixtures from Phase 0 dumps).
3. **Manual checklist** per phase (in `extension/TESTING.md`), executed with two real phones.
4. **DOM dumps:** add a hidden dev action "Dump DOM" (side panel, dev mode) that saves
   `document.documentElement.outerHTML` of the active WhatsApp frame/window to the server
   (`POST /api/dev/dump` → `data/dumps/<ts>_<label>.html`). Use it for every new selector.

---

## 12. Build plan (build-first: no stops; one test pass at the end)

Phase 0 (spike) and Phase 1 (server core) are done. Build B1→B7 in order, continuously. At the end of
each step: pytest + vitest green, plus a one-paragraph entry in `BUILD_LOG.md` (what was built, any
UNVERIFIED selectors, any deviation from this spec and why).

### B1: Extension project + real protocol
- Convert `extension/` to the §4 layout: TypeScript + Vite multi-entry build to `extension/dist`
  (inject, content, background, offscreen, popup, sidepanel, app), manifest per §5.2, plus
  `offscreen`, `tabCapture` and `scripting` permissions.
- Port the spike's inject.js logic into `src/inject/` **with the reviewer's fixes intact**: per-call
  mixer graph, tap nodes on a silent sink, `wrapCtor`, no ontrack override, per-node speaker taps,
  probes.
- Port content.js into `src/content/` with selectors in `selectors.ts` and the missed-ring direction
  fix.
- background: token pairing (§5.6), the real WS protocol (§6.1) to `phathom.server` (not the spike),
  reconnect/backoff, badge, notifications, single active WhatsApp tab.
- Keep `phathom serve --spike` working with the old spike extension in `extension/spike/` (moved, not
  deleted).

### B2: Caller audio sources (§5.7)
All three sources, the `caller_source` setting, server-side auto-pick (pure function, unit-tested with
synthetic per-tap VAD streams: a silent keep-alive tap, a mic-echo tap and a caller tap must resolve to
the caller), kind `0x04`, per-tap wavs, and end-of-call re-pick before the pipeline.

### B3: Calls end to end
Connect/Disconnect; `decide()` drives accept/ignore; content clicks Accept/End call; listen mode
(disclosure + record); Copilot on incoming and outgoing calls; pipeline after each call;
`summary_ready` notification. Peer name for outgoing calls: derive the selector from
`data/dumps/*.html`, or mark it UNVERIFIED (rule 3). Add a fake-extension pytest that drives a full
call over the WS with fixture wavs and asserts that a call record with correct speakers is created.

### B4: Talk mode
`LiveAgent` + `WsPlayer` over the extension, bot playback in inject.js (§5.3d), barge-in, Take over /
Hand back / Hang up. Setting `talk_mode_enabled` (default **on**). Diagnostics shows "Bot voice
path: unverified" until the user marks the test tone as heard in Diagnostics (one checkbox stored in
state). If the user marks it **not** heard, Talk is disabled in the UI with a short explanation.

### B5: UI
Popup, side panel and dashboard exactly per §7 (incl. §7.5 Diagnostics): React + Tailwind, tokens,
Inter, dark + light, skeleton/empty/error states, ⌘K. Every CLI feature reachable. Use mock data in
an isolated Storybook-free `ui-preview.html` (dev only) so pages can be checked without a call.
Produce screenshots of every page in both themes (Playwright against `ui-preview.html` is fine) into
`docs/screenshots/`.

### B6: Brain (§8)
Export-chat import (Android + iOS parsers with fixtures), message/chat/brain tables, brain building,
Brain page, Ask over chats, side-panel brain card on ring, `LiveAgent` context. Read-this-chat:
selectors from dumps, else UNVERIFIED (rule 3).

### B7: Cleanup + handover
- README section "v2 setup": `npm install && npm run build`, Load unpacked `extension/dist`,
  `phathom serve`, `phathom token` → paste in the extension.
- A systemd **user** service for `phathom serve`. Keep `phathom start --legacy`.
- `BUILD_LOG.md` final section: everything UNVERIFIED, every ⚠️ with which fallback exists, and the
  exact final test steps.

Task calls (§9) are **not** part of this build.

### Final test checklist (run once by the user after B7; the reviewer checks the results)

The tester opens Settings → Diagnostics, makes these calls with a second phone, and after each one
clicks **Copy diagnostics**:

1. **Disconnected**: phone calls in → it rings normally, nothing is answered, nothing is recorded.
2. **Copilot, incoming**: I answer. The caller talks 10 s while I'm silent, then I talk 10 s → summary
   appears; the transcript has correct You/Caller labels; Diagnostics shows the chosen caller source.
3. **Copilot, outgoing**: I call the phone → same checks, and the peer name is correct.
4. **Listen**: I don't answer → Phathom answers after the delay, the disclosure is heard on the
   phone, and the summary appears.
5. **Talk**: I don't answer → a 1-minute conversation in English, then Urdu; barge-in works; Take
   over works. Test tone result recorded in Diagnostics.
6. **UI**: every page loads with real data; Ask answers with citations.
7. **Brain**: import one exported chat → the brain appears; Ask uses it.

---

## 13. Out of scope (v2)

Group calls (beyond not crashing), voice cloning, voice-note transcription, Chrome Web Store
publishing, mobile, multi-user, cloud hosting, any paid API.

## 14. Definition of done

- B1–B7 built; `pytest` and `vitest` green; `BUILD_LOG.md` complete.
- Final test checklist run, and the reviewer's fixes applied.
- With the server running and the extension connected: missed calls are answered per mode,
  the user's own calls (in and out) are summarized with correct speakers, everything is browsable,
  searchable and askable from the dashboard, and Disconnect guarantees nothing is answered or recorded.
