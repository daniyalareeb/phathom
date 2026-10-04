// background.js — MV3 service worker. Phase 0 spike.
// Holds ONE WebSocket to the server (ws://127.0.0.1:8765/ws), forwards tab events/audio,
// routes popup commands (play test tone, dump DOM) to the WhatsApp tab.
const WS_URL = 'ws://127.0.0.1:8765/ws';
const BACKOFFS = [1, 2, 5, 10, 30];
let ws = null;
let backoffIdx = 0;
let seq = { caller: 0, owner: 0, speaker: 0 };
let lastCallActive = false;

function log(...a) { try { console.log('[PHATHOM bg]', ...a); } catch (e) {} }

function setBadge(state) {
  try {
    if (state === 'call') { chrome.action.setBadgeText({ text: '●' }); chrome.action.setBadgeBackgroundColor({ color: '#dc2626' }); }
    else if (state === 'ok') { chrome.action.setBadgeText({ text: '●' }); chrome.action.setBadgeBackgroundColor({ color: '#10b981' }); }
    else { chrome.action.setBadgeText({ text: '' }); }
  } catch (e) {}
}

function b64ToBytes(b64) {
  try {
    const s = atob(b64);
    const u = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    return u;
  } catch (e) { return null; }
}

function wsSend(obj) {
  try {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  } catch (e) {}
}

function sendAudio(track, pcmBase64) {
  try {
    const t = String(track);
    if (t.startsWith('speaker')) {
      // STEP 1A: keep the speaker:N label so the server stores one wav per tap.
      // JSON relay (base64) instead of the binary frame, which has no label field.
      wsSend({ t: 'audio', track: t, pcmBase64 });
      return;
    }
    const bytes = b64ToBytes(pcmBase64);
    if (!bytes || bytes.length === 0) return;
    const kind = t.startsWith('owner') ? 0x02 : 0x01;
    const key = kind === 0x02 ? 'owner' : 'caller';
    const n = (seq[key] = (seq[key] + 1) >>> 0);
    const frame = new Uint8Array(1 + 4 + bytes.length);
    frame[0] = kind;
    frame[1] = n & 0xff; frame[2] = (n >> 8) & 0xff; frame[3] = (n >> 16) & 0xff; frame[4] = (n >> 24) & 0xff;
    frame.set(bytes, 5);
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(frame);
  } catch (e) {}
}

function connectWs() {
  try { if (ws) { try { ws.close(); } catch (e) {} ws = null; } } catch (e) {}
  log('connecting to ' + WS_URL);
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) { scheduleReconnect(); return; }
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    log('WS open');
    backoffIdx = 0;
    setBadge('ok');
    wsSend({ t: 'hello', ext_version: '0.0.0-spike' });
  };
  ws.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      if (d && d.t === 'ping') wsSend({ t: 'pong' });
      // Spike server sends nothing else; future commands (accept/play) ride here.
    } catch (e) {}
  };
  ws.onclose = () => { log('WS closed, retrying'); setBadge('idle'); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

function scheduleReconnect() {
  const s = BACKOFFS[Math.min(backoffIdx, BACKOFFS.length - 1)];
  backoffIdx++;
  setTimeout(connectWs, s * 1000);
}
connectWs();
setInterval(() => wsSend({ t: 'ping' }), 20000);

// Long-lived ports from content.js tabs (audio + events).
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'audio') return;
  port.onMessage.addListener((msg) => {
    try {
      if (!msg || !msg.t) return;
      if (msg.t === 'audio') {
        sendAudio(msg.track, msg.pcmBase64);
        return;
      }
      if (msg.t === 'call_started') lastCallActive = true;
      if (msg.t === 'call_ended') lastCallActive = false;
      setBadge(msg.t === 'call_started' ? 'call' : (lastCallActive ? 'call' : 'ok'));
      wsSend(msg);
    } catch (e) {}
  });
});

// Popup / command messages: forward to the WhatsApp tab.
async function waTabs() {
  try {
    const tabs = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
    return tabs || [];
  } catch (e) { return []; }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg && msg.cmd === 'play_tone') {
        const tabs = await waTabs();
        if (!tabs.length) { sendResponse({ ok: false, error: 'no WhatsApp tab' }); return; }
        const r = await chrome.tabs.sendMessage(tabs[0].id, { cmd: 'play_tone', freq: 440, durationS: 3 });
        log('play_tone -> tab', r);
        sendResponse({ ok: true });
      } else if (msg && msg.cmd === 'dump_dom') {
        const tabs = await waTabs();
        if (!tabs.length) { sendResponse({ ok: false, error: 'no WhatsApp tab' }); return; }
        // Ask EVERY matching tab/frame; each content.js reports its own frame href.
        const results = [];
        for (const t of tabs) {
          try { results.push(await chrome.tabs.sendMessage(t.id, { cmd: 'dump_dom', label: msg.label || 'manual' })); }
          catch (e) { results.push({ ok: false }); }
        }
        sendResponse({ ok: true, results });
      } else if (msg && msg.cmd === 'bg_status') {
        sendResponse({ ok: true, ws: ws ? ws.readyState : -1, lastCallActive });
      }
    } catch (e) {
      try { sendResponse({ ok: false, error: String(e) }); } catch (e2) {}
    }
  })();
  return true;
});

// Keyboard command (see manifest.commands) -> test tone.
try {
  chrome.commands.onCommand.addListener((cmd) => {
    if (cmd === 'play-test-tone') {
      (async () => {
        const tabs = await waTabs();
        for (const t of tabs) {
          try { await chrome.tabs.sendMessage(t.id, { cmd: 'play_tone', freq: 440, durationS: 3 }); } catch (e) {}
        }
      })();
    }
  });
} catch (e) {}
