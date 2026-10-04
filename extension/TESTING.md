# Extension manual test checklist (EXTENSION_SPEC §11.3)

Run with two real phones. Open `app.html#/settings` → Diagnostics for every step
and click **Copy diagnostics** after each call.

## Load

1. `npm install && npm run build` → `dist/` contains `inject.js`, `content.js`,
   `background.js`, `offscreen.js` (each with **zero** `import` statements —
   MV3 content scripts cannot load shared chunks), `offscreen.html`, `popup.html`,
   `sidepanel.html`, `app.html`, `manifest.json`, `worklets/pcm-tap.js`.
2. `phathom serve` (note the token via `phathom token`).
3. Chrome → `chrome://extensions` → Developer mode → Load unpacked → `dist/`.
4. Open the popup → first-run card → paste token → CONNECTED.

## B1 checks

- [ ] Popup shows CONNECTED; badge is green; `ws` = 1 in background status.
- [ ] WhatsApp Web tab open → Settings → Diagnostics → hooks: gum + pc true,
      worklet = `worklet`, `wa.logged_in` = true.
- [ ] Popup → Side panel button opens the live companion.
- [ ] `vitest run` green (audio/protocol/selectors + spike legacy tests).

## B2 checks (caller sources)

- [ ] Diagnostics → caller source shows mode `auto` and the active source.
- [ ] Per-tap level meters move during a call (`speaker:N`, `tab`, `webrtc`, owner).

## B3 checks (calls end to end)

- [ ] Disconnected: incoming call rings normally, nothing answered/recorded.
- [ ] Copilot incoming/outgoing: summary appears; You/Caller labels correct.
- [ ] Outgoing peer name: flagged UNVERIFIED until a DOM dump confirms it.

## B4 checks (talk)

- [ ] Play test tone → heard on the phone → tick the checkbox in Diagnostics.
- [ ] If NOT heard: Talk disables itself in the UI with an explanation.

## B5 checks (UI)

- [ ] Every dashboard page loads with real data; ⌘K palette works; both themes.
- [ ] Screenshots in `docs/screenshots/` match the build.

## B6 checks (brain)

- [ ] Import one exported `.txt`/`.zip` chat → brain appears; Ask cites chats.
