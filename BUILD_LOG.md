# MyPhathom v2 — BUILD LOG (B1 → B7, build-first mode)

Conventions: every step ended with `pytest` + `vitest` green. Selectors come
from `phathom/whatsapp/selectors.py` or `data/dumps/*.html`; anything else is
marked `// UNVERIFIED: needs DOM dump`, fails visibly (Diagnostics row + log),
and is listed below. `agent.py` received no logic changes (verified by import).

## B1 — Extension project + real protocol ✅ (pytest 63, vitest 20)

- Moved the Phase-0 spike extension to `extension/spike/` (unchanged, still
  imported by its own vitest file). `phathom serve --spike` untouched.
- New TypeScript + Vite multi-entry build (`extension/vite.build.ts`) to
  `extension/dist/`: `inject.js`, `content.js`, `background.js`,
  `offscreen.js` (each bundles to ONE self-contained file — verified zero
  `import` statements, since MV3 content scripts cannot load shared chunks),
  plus `popup.html`, `sidepanel.html`, `app.html`, `preview.html`.
  Manifest per §5.2 + `offscreen`, `tabCapture`, `scripting` permissions.
- Ported spike logic with all reviewer fixes intact: fresh mixer graph per
  getUserMedia call, tap nodes on a silent sink, `wrapCtor()`
  (Reflect.construct + new.target) for AudioContext/AudioWorkletNode/Worker,
  no `pc.ontrack` override (addEventListener only), per-node speaker taps,
  RING_GRACE_S missed-ring → outgoing direction fix.
- Content selectors in `src/content/selectors.ts`, ported 1:1 from
  `selectors.py`; verified by `test/selectors.test.ts` against the Python file.
- Background: token pairing (storage + `?token=`), real WS protocol to
  `phathom serve` (hello/wa_status/ring/call_started/call_ended/played/
  ui_action/ping), reconnect backoff 1/2/5/10/30 s, 20 s ping, badge states,
  notifications, most-recently-focused active tab, 5-min PCM buffer flushed on
  `call_started`, side-panel auto-open, `chrome.alarms` keepalive.
- `extension/TESTING.md` manual checklist.

## B2 — Caller audio sources (§5.7) ✅

- New `caller_source` setting (`auto` default | `webrtc` | `speaker_tap` |
  `tab_capture`), validated in `runtime_settings.set_many`.
- New pure module `phathom/server/caller_pick.py`: `score_taps` (exclusive
  voice = voiced while owner silent), `pick_caller`, `resolve_source` (auto
  order webrtc → tab → speaker). Unit-tested with synthetic streams: silent
  keep-alive scores 0, mic-echo scores 0, caller tap wins.
- Binary kind `0x04` = tab (`KIND_TAB`) end to end (ext → `CallSession.feed`).
- `CallSession`: per-track VAD buckets (100 ms), live re-pick every 5 s,
  only the selected source reaches the agent queue, per-tap `dBFS` meters,
  per-tap wavs under `data/recordings/<call_id>/taps/`, whole-call re-pick
  after sinks close (winner replaces `caller.wav`, live kept as
  `caller_live.wav`). Taps pruned after 7 days in `retention_cleanup`.
- `GET /api/diagnostics`: hooks, worklet mode, WA status, caller
  source + per-tap meters, bot-voice path, selector check, server bits.
- Extension already had all three sources from B1 (webrtc tap, per-node
  speaker taps, offscreen tabCapture with speaker playback); added the
  `caller_source` select + Diagnostics meters UI in Settings.

## B3 — Calls end to end ✅ (new `test_full_call.py`, 5 tests)

- `decide()` → accept/ignore wiring verified: ring (mode notes, allowed
  contact, delay 0) sets `_expect_bot` and sends `accept`; blocked contact
  sends nothing.
- Bot-answer attribution: background sets `pendingBotAnswer` on `accept` and
  rewrites the next `call_started.answered_by` to `"bot"` (content.js cannot
  know who clicked).
- Fake-extension full calls with fixture wavs: incoming listen (disclosure
  played + acked, `caller.wav` recorded, pipeline mode `notes`) and outgoing
  copilot (caller.wav + owner.wav, correct speakers, pipeline mode `copilot`).
- Disconnect: `PATCH /state {connected:false}` calls
  `manager.disconnect_gracefully()` — bot calls get `hangup` + ingest close
  (pipeline still runs); copilot ingest closes with NO hangup (call continues).
- Outgoing peer name: tries `OUTGOING_PEER_CANDIDATES` in order, reports
  `outgoing_peer_unverified` diag when guessing (see UNVERIFIED).

## B4 — Talk mode ✅ (new `test_talk.py`, 4 tests)

- `WsPlayer.muted`: `play()` resolves False with zero bytes sent when muted;
  `interrupt()` only sends `stop_audio` when something is actually playing
  (no stop storms; existing interrupt test still passes).
- `take_over()`: mutes player + interrupts + starves the agent queue
  (agent stops hearing replies) while sinks keep recording; `hand_back()`
  restores. Zero `agent.py` changes.
- Bot PCM path: server binary `0x11` → background (`lastPlayId` from
  `play_begin`) → tab `play_chunk` → inject gap-free 24 kHz scheduling →
  `played{id}` ack; `stop_audio` kills all sources (barge-in).
- Talk gating: `talk_mode_enabled` (default on); when off, bot answers fall
  back to `listen` with a loud log. `test_tone_heard` setting backs the
  Diagnostics checkbox ("Bot voice path: verified/unverified"); unchecking
  it is wired to also disable Talk in the UI with an explanation.

## B5 — UI ✅

- Popup (§7.2: Connect toggle, Listen/Talk/Off, Copilot, status chips, last
  call, first-run token card, server-down card), side panel (§7.3: idle
  brief, ringing countdown + brain card, live transcript, Take over/Hand
  back/Hang up/Note, dev Diagnostics), dashboard (§7.4: Home, Calls + detail
  with track switch + seek-click transcript, Ask with citations, Brain,
  To-dos, Contacts, Settings incl. Diagnostics + CLI map).
- Design tokens in `tokens.css` (dark-first + light, system follow +
  override), Inter bundled as woff2 in `public/fonts` (fontsource removed
  after Vite refused to bundle its subset files), one emerald accent,
  skeleton/empty/error states everywhere, ⌘K palette, focus rings, reduced
  motion respected.
- Dev-only `preview.html` mock page; 24 Playwright screenshots (12 pages ×
  light/dark) in `docs/screenshots/` (script: `scripts/screenshots.py`).
- Deviation: Tailwind v4 (CSS-first `@import "tailwindcss"`, no
  `tailwind.config.js`) — tokens remain the single source of truth, consumed
  as CSS variables.

## B6 — Brain (§8) ✅ (new `test_brain.py`, 8 tests)

- `phathom/brain.py`: Android (`dd/mm/yyyy, hh:mm - Name:`) + iOS
  (`[dd/mm/yyyy, hh:mm:ss] Name:`) parsers, 12/24 h, multi-line, system-line
  filtering; `.txt`/`.zip` import; `sha1(ts+sender+text)` dedup;
  windowed extract (cheap model) + smart merge (dedupe ≤30, newest wins,
  summary ≤120 words) with deterministic local-merge fallback; per-fact
  `message_ts` sources; `brain_context_for` (cap 1,500 chars).
- Schema + CRUD in `store.py` (all `DEFINE`s verified live against SurrealDB
  before committing, incl. `msg_dedup UNIQUE` and `msg_text_ft FULLTEXT`).
  Dedup race note: the UNIQUE index raises `InternalError("already
  contains")` — handled as skip, not failure.
- API (§8.5): list/get/import/messages/rebuild/delete; Ask covers
  `message` + `brain` tables with `chat` citations (CLI `ask` too).
- Uses: Brain pages, Ask chat citations, side-panel brain card on ring,
  `LiveAgent` context via the notes string only (`agent.py` untouched).
- Read-this-chat: content.js scrolls ≥800 ms/step, ≤5,000 msgs, progress +
  cancel path; bubble/timestamp selectors UNVERIFIED (see below).

## B7 — Cleanup + handover ✅

- README "v2 setup" section (6 commands + where to click); `phathom start
  --legacy` flag added (daemon IS the legacy path); `serve --spike` smoke-
  tested (boots, serves); new `systemd/phathom-serve.service` (user unit,
  After phathom-db).
- Final: pytest 87 green, vitest 20 green, `tsc` clean, `dist/` rebuilt,
  screenshots regenerated.

## Post-build fixes (2026-10-03)

- `offscreen.html` was never emitted to `dist/` (not a Vite input, and its
  script pointed at the `.ts` source) → `tab_capture` always failed. Moved to
  `public/offscreen.html`, loading the built `offscreen.js`.
- §10 "hooks failed" gate was missing: `ExtensionManager.hooks_failed()`
  (reported `gum:false`) now blocks auto-answer and Copilot. Test added
  (pytest 88).

## Embedded DB + light UI (2026-10-04)

- SurrealDB now runs embedded inside `phathom serve` (`SURREAL_URL=surrealkv://data/db`,
  default): one command, no DB process or password. One shared connection per
  process (two engine instances on the same files don't see each other's
  writes); an flock on `data/db.lock` keeps a second process out with a clear
  message; opening/bootstrap is serialized (parallel DEFINEs corrupted reads:
  "Invalid revision"); queries are serialized with retry (optimistic-txn
  conflicts). Schema falls back to 2.x syntax (`SEARCH ANALYZER`). ws:// still
  works for an external server. Tests use `mem://`.
- Fixed: brain `facts/open_loops/decisions/people/timeline` were silently
  dropped (plain `TYPE array` on SCHEMAFULL) → `FLEXIBLE TYPE array`.
- Fixed: Ask crashed (KeyError) whenever chat/brain evidence matched.
- Fixed: call audio never played (relative `<audio src>` without the token)
  → authenticated fetch + object URL.
- Fixed: side panel "Let it ring" sent hangup (ignored while ringing) → new
  `ignore` ui_action; "Answer now" now goes through the server (`answer_now`)
  so the call is attributed to the bot.
- UI redesign, light only: Schibsted Grotesk / Inter / IBM Plex Mono, one
  colour per speaker, transcript speaker rail with click-to-seek + playing-line
  highlight, talk-share bar. Added the missing §7 pieces (summary state, rule
  badge, mode chip + timer, editable title, action-item checkboxes, day groups,
  contact/date filters, messages-for-you, to-dos by person, contact drawer).
  Mock calls/recordings and sample contacts removed. pytest 93.

## UNVERIFIED items (need a real DOM dump via Diagnostics → Dump DOM)

1. `OUTGOING_PEER_CANDIDATES` (3 candidates) — outgoing calls show the peer
   name but flagged unverified; `outgoing_peer_unverified` diag fires.
2. `CHAT_PANE` / `CHAT_BUBBLE` (Brain read-this-chat) — import path works
   without them; the reader returns [] + visible "no selectors" state until
   a dump confirms them.
3. `open_chat` action — refuses loudly until (1)/(2) are verified.
4. `QR_CODE` selector (logged-out state never observed) — carried over.

## ⚠️ fallbacks (setting with `auto` default + Diagnostics visibility)

- Caller voice: `webrtc` → `tab_capture` (needs one Connect click per
  browser restart; else falls back to `speaker_tap`) → `speaker_tap`
  auto-pick (exclusive-voice scoring + whole-call re-pick). Choice shown in
  Diagnostics with per-tap dB meters.
- Bot voice into the call: primary mixer substitution + test-tone checkbox;
  if not heard, Talk disables with explanation (listen + Copilot unaffected).
- Worklet vs ScriptProcessor: runtime fallback with identical framing.
- Groq 429: existing retries + live-transcript 60 s pause + pipeline
  `reprocess`; LLM merge falls back to deterministic local merge.

## Known gaps (not built, per spec)

- Task calls (§9) — explicitly out of scope for this build.
- Group calls: extra remote tracks logged, only `caller:1` recorded.
- Voice-note transcription, voice cloning, Web Store publishing — out of scope.
