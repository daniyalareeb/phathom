# MyPhathom

Local assistant that auto-answers your WhatsApp calls (WhatsApp Web), talks to
the caller as your AI assistant, records + transcribes, and answers questions
about past calls later. Build spec: `SPEC.md` (wins on conflicts), background: `PLAN.md`.

## Everyday use (v2: extension + local server)

```bash
cd ~/MyProjects/MyPhathom
.venv/bin/phathom serve      # starts Phathom (API + embedded database). Ctrl+C stops it.
```

That's the whole routine: open a terminal, start it, use the extension, press
Ctrl+C when you're done. The database runs inside `phathom serve` from
`data/db`; there is no separate database process and no DB password.
`phathom calls/show/ask/status` read the same data from the terminal while the
server is stopped (while it runs, use the dashboard).

## Setup (once)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env   # then put your GROQ_API_KEY in .env
.venv/bin/phathom setup     # ✅/❌ table: tools, key, DB schema
.venv/bin/phathom login     # scan the QR in the Chrome window, wait for chats
```

Legacy v1 daemon (Playwright/PulseAudio):

```bash
.venv/bin/phathom start            # foreground; Ctrl+C stops
.venv/bin/phathom start --background
```

## v2 setup (extension + local server)

The v2 path runs Phathom inside your own Chrome via an extension. The v1
Playwright/PulseAudio path keeps working behind `phathom start --legacy`.

```bash
.venv/bin/phathom serve                             # API+WS on 127.0.0.1:8765, DB embedded
.venv/bin/phathom token                             # print the extension token
cd extension && npm install && npm run build        # build to extension/dist/
```

Chrome → `chrome://extensions` → Developer mode → Load unpacked →
`extension/dist/` (the `dist` folder, not `extension/`). Open the popup → paste the token once → CONNECTED.
Dashboard: `app.html` (options page) or the popup's "Open dashboard" button.

Autostart the server instead of the daemon:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/phathom-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now phathom-serve.service
```

To use an external SurrealDB instead, set `SURREAL_URL=ws://…/rpc` (plus
`SURREAL_USER`/`SURREAL_PASS`) in `.env`; `scripts/run_db.sh` still starts a
local one.

`phathom serve --spike` still runs the Phase-0 capture server (old spike
extension kept in `extension/spike/`).

## Daily use

```bash
.venv/bin/phathom mode notes|assistant|off
.venv/bin/phathom brief "If Ahmed asks about the report, say it will be sent tonight."
.venv/bin/phathom brief --append "Second line."
.venv/bin/phathom status
.venv/bin/phathom calls --limit 20
.venv/bin/phathom show <call_id>
.venv/bin/phathom ask "what did Ahmed want?"
.venv/bin/phathom stop
```

`contacts.yaml`: `policy: all|allowlist`, `block:` labels, per-caller `notes:`.
`brief.md`: today's instructions, re-read on every call.

## Autostart (systemd user services)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/phathom.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now phathom.service
loginctl enable-linger "$USER"   # keep running after logout (optional)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Phathom: WhatsApp logged out` | `phathom login`, scan QR again |
| `WhatsApp UI changed — rerun discovery` (or clicks miss) | `.venv/bin/python scripts/discover.py --mode watch`, place a test call, update `phathom/whatsapp/selectors.py` from `data/debug/` dumps. All selectors live in that one file; class names are obfuscated — use role/testid only |
| `Microphone unavailable` in the bot's Chrome | The launcher points Chrome at `phathom_mic`; that source only exists while devices are up. It is created by `test-audio`/daemon automatically — don't set `PULSE_*` manually |
| Groq 429 / rate limits | Retried with back-off + model fallback automatically. Free-tier OTPM caps are tight (`qwen` ≤ 1000 output tokens); heavy days may need a pause |
| Groq TTS fallback never triggers | Expected: `orpheus-v1-english` needs org-terms acceptance in the Groq console, `playai-tts` is decommissioned. edge-tts (EN+UR) is the working path |
| `phathom process` failed | Audio is kept in `data/recordings/<id>/`; re-run `phathom process <wav> --caller NAME` |
| DB was down during a call | Pipeline writes `data/recordings/<id>/result.json`; import with `phathom process --from-json <path>` |
| No sound / wrong routing | `scripts/audio_setup.sh status`; `phathom test-audio` (checks levels + restores defaults) |

## Models (verified Oct 2026)

Spec'd Llama models are gone from Groq. In use: STT `whisper-large-v3-turbo`
(live) / `whisper-large-v3` (final), live replies `openai/gpt-oss-20b`
(no constrained-JSON — parses instruct-following output), summaries + Q&A
`qwen/qwen3.8-27b`, TTS edge-tts (`en-US-AndrewNeural` / `ur-PK-AsadNeural`).
