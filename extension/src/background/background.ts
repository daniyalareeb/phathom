// background.ts — MV3 service worker (EXTENSION_SPEC §5.5, §5.6, §5.7).
// Single WS to the server, reconnect/backoff, badge, notifications, active-tab
// routing, bot-audio fan-out, offscreen tabCapture lifecycle.

import { encodeAudioFrame, KIND_BOT, KIND_CALLER, KIND_OWNER, KIND_SPEAKER, KIND_TAB, b64ToBytes } from '../shared/protocol';

const WS_PATH = '/ws';
const BACKOFFS = [1, 2, 5, 10, 30];
const EXT_VERSION = '0.2.0';

let ws: WebSocket | null = null;
let backoffIdx = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let seq = { caller: 0, owner: 0, speaker: 0, tab: 0 };
let lastCallActive = false;
let serverState: Record<string, unknown> | null = null;
let activeTabId: number | null = null;
let audioBuffer: { track: string; pcm: Uint8Array }[] = [];
let waLoggedIn: boolean | null = null;
// Latest bot utterance id (play_begin … play_end); binary 0x11 chunks attach here.
let lastPlayId: number | null = null;
// Set when the server orders `accept`: the next call_started was answered by
// the bot (content.js always reports owner — it cannot know who clicked).
let pendingBotAnswer = false;

function log(...a: unknown[]) {
  try {
    console.log('[PHATHOM bg]', ...a);
  } catch {
    /* noop */
  }
}

async function serverBase(): Promise<string> {
  try {
    const v = await chrome.storage.local.get(['serverUrl']);
    return (v.serverUrl as string) || 'http://127.0.0.1:8765';
  } catch {
    return 'http://127.0.0.1:8765';
  }
}

async function token(): Promise<string | null> {
  try {
    const v = await chrome.storage.local.get(['phathomToken']);
    return (v.phathomToken as string) || null;
  } catch {
    return null;
  }
}

function setBadge(state: 'idle' | 'ok' | 'call' | 'error') {
  try {
    if (state === 'call') {
      chrome.action.setBadgeText({ text: '●' });
      chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
    } else if (state === 'ok') {
      chrome.action.setBadgeText({ text: '●' });
      chrome.action.setBadgeBackgroundColor({ color: '#10b981' });
    } else if (state === 'error') {
      chrome.action.setBadgeText({ text: '!' });
      chrome.action.setBadgeBackgroundColor({ color: '#f59e0b' });
    } else {
      chrome.action.setBadgeText({ text: '●' });
      chrome.action.setBadgeBackgroundColor({ color: '#9ca3af' });
    }
  } catch {
    /* noop */
  }
}

function wsUrl(base: string, tok: string | null): string {
  const u = base.replace(/^http/, 'ws') + WS_PATH;
  return tok ? `${u}?token=${encodeURIComponent(tok)}` : u;
}

function wsSend(obj: unknown) {
  try {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  } catch {
    /* noop */
  }
}

function kindForTrack(track: string): { kind: number; key: keyof typeof seq } | null {
  const t = String(track);
  if (t.startsWith('owner')) return { kind: KIND_OWNER, key: 'owner' };
  if (t.startsWith('tab')) return { kind: KIND_TAB, key: 'tab' };
  if (t.startsWith('speaker')) return { kind: KIND_SPEAKER, key: 'speaker' };
  return { kind: KIND_CALLER, key: 'caller' };
}

function sendAudio(track: string, pcmBase64: string) {
  try {
    // Speaker taps keep their full label via JSON (binary frames have no label).
    if (String(track).startsWith('speaker')) {
      wsSend({ t: 'audio', track, pcmBase64 });
      return;
    }
    const bytes = b64ToBytes(pcmBase64);
    if (!bytes || bytes.length === 0) return;
    const k = kindForTrack(track);
    if (!k) return;
    const n = (seq[k.key] = (seq[k.key] + 1) >>> 0);
    const frame = encodeAudioFrame(k.kind, n, bytes);
    if (lastCallActive) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(frame.buffer as ArrayBuffer);
    } else {
      // Buffer up to 5 min of PCM across reconnects (§10); flush on call start.
      audioBuffer.push({ track, pcm: bytes });
      if (audioBuffer.length > 3000) audioBuffer.shift();
    }
  } catch {
    /* noop */
  }
}

function flushAudioBuffer() {
  try {
    for (const { track, pcm } of audioBuffer.splice(0)) {
      const k = kindForTrack(track);
      if (!k || !ws || ws.readyState !== WebSocket.OPEN) continue;
      const n = (seq[k.key] = (seq[k.key] + 1) >>> 0);
      ws.send(encodeAudioFrame(k.kind, n, pcm).buffer as ArrayBuffer);
    }
  } catch {
    /* noop */
  }
}

async function connectWs() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    // Detach first: the old socket's onclose must not schedule another
    // reconnect (that closed the new socket → a 1-second reconnect loop).
    const old = ws;
    ws = null;
    old.onopen = old.onmessage = old.onclose = old.onerror = null;
    try {
      old.close();
    } catch {
      /* noop */
    }
  }
  const base = await serverBase();
  const tok = await token();
  if (!tok) {
    log('no token yet; waiting for pairing (first-run screen)');
    setBadge('idle');
    return;
  }
  const url = wsUrl(base, tok);
  log('connecting to ' + url.replace(/token=.*/, 'token=…'));
  let sock: WebSocket;
  try {
    sock = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }
  ws = sock;
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    if (ws !== sock) return;
    log('WS open');
    backoffIdx = 0;
    setBadge(serverState ? 'ok' : 'ok');
    audioBuffer = [];
    wsSend({ t: 'hello', ext_version: EXT_VERSION, tab_id: activeTabId });
  };
  ws.onmessage = (ev: MessageEvent) => {
    try {
      if (ev.data instanceof ArrayBuffer) {
        void forwardBotChunk(ev.data);
        return;
      }
      if (typeof ev.data !== 'string') return;
      const d = JSON.parse(ev.data) as Record<string, unknown>;
      void handleServerMsg(d);
    } catch {
      /* noop */
    }
  };
  ws.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    log('WS closed, retrying');
    setBadge('error');
    scheduleReconnect();
  };
  ws.onerror = () => {
    try {
      sock.close();
    } catch {
      /* noop */
    }
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return; // one pending reconnect at a time
  const s = BACKOFFS[Math.min(backoffIdx, BACKOFFS.length - 1)];
  backoffIdx++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connectWs();
  }, s * 1000);
}

async function waTabs(): Promise<chrome.tabs.Tab[]> {
  try {
    return (await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' })) || [];
  } catch {
    return [];
  }
}

async function sendToActiveTab(msg: Record<string, unknown>): Promise<unknown> {
  const tabs = await waTabs();
  if (!tabs.length) return { ok: false, error: 'no WhatsApp tab' };
  // Prefer the most recently focused tab (single active tab rule §5.4d).
  const tab = [...tabs].sort((a, b) => ((b.lastAccessed ?? 0) as number) - ((a.lastAccessed ?? 0) as number))[0];
  activeTabId = tab.id ?? null;
  return chrome.tabs.sendMessage(tab.id!, msg);
}

async function handleServerMsg(d: Record<string, unknown>) {
  try {
    const t = d.t as string;
    if (t === 'ping') {
      wsSend({ t: 'pong' });
      return;
    }
    if (t === 'welcome' || t === 'state') {
      serverState = (d.state as Record<string, unknown>) || null;
      setBadge(lastCallActive ? 'call' : 'ok');
      broadcastUi({ kind: 'server-state', state: serverState });
      return;
    }
    if (t === 'accept' || t === 'hangup') {
      if (t === 'accept') pendingBotAnswer = true; // next call_started is bot-answered
      await sendToActiveTab({ cmd: t });
      return;
    }
    if (t === 'set_gain') {
      await sendToActiveTab({ cmd: 'set_gain', mic: d.mic, bot: d.bot });
      return;
    }
    if (t === 'play_begin') {
      lastPlayId = Number(d.id);
      await sendToActiveTab({ cmd: 'play_begin', id: d.id });
      return;
    }
    if (t === 'play_end') {
      await sendToActiveTab({ cmd: 'play_end', id: d.id });
      if (lastPlayId === Number(d.id)) lastPlayId = null;
      return;
    }
    if (t === 'stop_audio') {
      lastPlayId = null;
      await sendToActiveTab({ cmd: 'stop_audio' });
      return;
    }
    if (t === 'live') {
      const ev = d.event as { type: string } | undefined;
      if (ev?.type === 'call_started') broadcastUi({ kind: 'call-started', event: ev });
      else if (ev?.type === 'call_ended' || ev?.type === 'summary_ready') broadcastUi({ kind: 'call-ended', event: ev });
      else broadcastUi({ kind: 'live', event: ev, call_id: d.call_id });
      // Mirror live events to notifications where they matter.
      if (ev?.type === 'ring') notify('Incoming call', String((ev as unknown as { caller: string }).caller));
      if (ev?.type === 'summary_ready') notify('Summary ready', 'Open the dashboard to read it.');
      return;
    }
    // Bot binary chunks arrive as ArrayBuffer with a pending play id — but the
    // server sends bot audio as binary frames; route them to the active tab.
    if (t === 'play_chunk') {
      await sendToActiveTab({ cmd: 'play_chunk', id: d.id, pcm: d.pcm });
      return;
    }
  } catch {
    /* noop */
  }
}

/** Binary server→ext frames (bot PCM 0x11): forward to the tab as play chunks. */
async function forwardBotChunk(buf: ArrayBuffer) {
  try {
    const u = new Uint8Array(buf);
    if (u.length < 5 || u[0] !== KIND_BOT || lastPlayId == null) return;
    await sendToActiveTab({ cmd: 'play_chunk', id: lastPlayId, pcm: buf });
  } catch {
    /* noop */
  }
}

function notify(title: string, message: string) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/connected-48.png',
      title: `Phathom · ${title}`,
      message,
    });
  } catch {
    /* noop */
  }
}

function broadcastUi(msg: Record<string, unknown>) {
  try {
    chrome.runtime.sendMessage({ ...msg, _bg: true }).catch(() => {});
  } catch {
    /* noop */
  }
}

// Long-lived ports from content.js tabs (audio + events).
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'audio') return;
  port.onMessage.addListener((msg: Record<string, unknown>) => {
    try {
      if (!msg || !msg.t) return;
      if (msg.t === 'audio') {
        sendAudio(String(msg.track), String(msg.pcmBase64));
        return;
      }
      if (msg.t === 'call_started') {
        lastCallActive = true;
        flushAudioBuffer();
        if (pendingBotAnswer) {
          pendingBotAnswer = false;
          (msg as Record<string, unknown>).answered_by = 'bot';
        }
      }
      if (msg.t === 'call_ended') lastCallActive = false;
      setBadge(msg.t === 'call_started' ? 'call' : lastCallActive ? 'call' : ws ? 'ok' : 'error');
      if (msg.t === 'ring') {
        // Auto-open side panel on ring when enabled (§5.5).
        void maybeOpenSidePanel();
      }
      if (msg.t === 'dom_dump') {
        // Forward DOM dumps to the server via REST (needs token).
        void forwardDump(msg);
      }
      wsSend(msg);
    } catch {
      /* noop */
    }
  });
});

async function maybeOpenSidePanel() {
  try {
    const auto = (serverState?.side_panel_auto as boolean) ?? true;
    if (!auto) return;
    const tabs = await waTabs();
    if (!tabs.length || tabs[0].id == null) return;
    await chrome.sidePanel.open({ tabId: tabs[0].id });
  } catch {
    /* noop */
  }
}

async function forwardDump(msg: Record<string, unknown>) {
  try {
    const [base, tok] = [await serverBase(), await token()];
    if (!tok) return;
    await fetch(base + '/api/dev/dump', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${tok}` },
      body: JSON.stringify({ label: msg.label, href: msg._frame, html: msg.html }),
    });
  } catch {
    /* noop */
  }
}

try {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});
} catch {
  /* noop */
}

// Popup / UI messages.
chrome.runtime.onMessage.addListener((msg: Record<string, unknown>, sender, sendResponse) => {
  (async () => {
    try {
      if (msg && (msg as { _bg?: boolean })._bg) return; // our own broadcast
      if (msg.cmd === 'play_tone') {
        const r = await sendToActiveTab({ cmd: 'play_tone', freq: 440, durationS: 3 });
        log('play_tone -> tab', r);
        sendResponse({ ok: true });
      } else if (msg.cmd === 'dump_dom') {
        const tabs = await waTabs();
        const results = [];
        for (const t of tabs) {
          try {
            results.push(await chrome.tabs.sendMessage(t.id!, { cmd: 'dump_dom', label: (msg.label as string) || 'manual' }));
          } catch {
            results.push({ ok: false });
          }
        }
        sendResponse({ ok: true, results });
      } else if (msg.cmd === 'accept' || msg.cmd === 'hangup_now') {
        const r = await sendToActiveTab({ cmd: msg.cmd === 'accept' ? 'accept' : 'hangup' });
        sendResponse(r);
      } else if (msg.cmd === 'ui_action') {
        wsSend({ t: 'ui_action', action: msg.action, text: msg.text });
        sendResponse({ ok: true });
      } else if (msg.cmd === 'connect_toggle') {
        // Tell the server; send even without WS (REST fallback handled by UI).
        wsSend({ t: 'ui_action', action: 'noop' });
        sendResponse({ ok: true, ws: ws ? ws.readyState : -1 });
      } else if (msg.cmd === 'ensure_capture') {
        const r = await ensureTabCapture();
        sendResponse(r);
      } else if (msg.cmd === 'bg_status') {
        sendResponse({
          ok: true,
          ws: ws ? ws.readyState : -1,
          lastCallActive,
          waLoggedIn,
          activeTabId,
          state: serverState,
        });
      } else if (msg.cmd === 'reconnect') {
        void connectWs();
        sendResponse({ ok: true });
      }
    } catch (e) {
      try {
        sendResponse({ ok: false, error: String(e) });
      } catch {
        /* noop */
      }
    }
  })();
  return true;
});

// --- tabCapture via offscreen document (§5.7 source 3) ---
let captureActive = false;

async function ensureTabCapture(): Promise<{ ok: boolean; active?: boolean; error?: string }> {
  try {
    const tabs = await waTabs();
    if (!tabs.length || tabs[0].id == null) return { ok: false, error: 'no WhatsApp tab' };
    const tabId = tabs[0].id;
    await chrome.offscreen.createDocument({
      url: 'offscreen.html',
      reasons: [chrome.offscreen.Reason.USER_MEDIA],
      justification: 'Capture WhatsApp tab audio for caller-voice fallback (tab_capture source).',
    }).catch(() => {});
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    await chrome.runtime.sendMessage({ cmd: 'offscreen_capture', streamId });
    captureActive = true;
    return { ok: true, active: true };
  } catch (e) {
    // Fresh-gesture requirement: fall back to speaker_tap (§5.7 ⚠️).
    return { ok: false, error: `tab capture unavailable, using speaker tap (${String(e).slice(0, 120)})` };
  }
}

chrome.runtime.onMessage.addListener((msg: Record<string, unknown>) => {
  if (msg.t === 'wa_status') waLoggedIn = Boolean(msg.logged_in);
});

// Keep-alive ping every 20 s (an open WS with traffic keeps MV3 workers alive).
setInterval(() => wsSend({ t: 'ping' }), 20000);

// Reconnect with backoff (1, 2, 5, 10, 30 s) — scheduled on close; also retry
// pairing when the token appears later.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.phathomToken || changes.serverUrl)) void connectWs();
});

try {
  chrome.alarms.create('phathom-keepalive', { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) void connectWs();
  });
} catch {
  /* noop */
}

try {
  chrome.commands.onCommand.addListener((cmd) => {
    if (cmd === 'play-test-tone') void sendToActiveTab({ cmd: 'play_tone', freq: 440, durationS: 3 });
  });
} catch {
  /* noop */
}

void connectWs();
setBadge('idle');
