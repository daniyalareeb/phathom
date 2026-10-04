// content.js — ISOLATED world, run_at document_idle, all_frames. Phase 0 spike.
// Call watcher ported 1:1 from phathom/whatsapp/selectors.py + call-end logic from
// phathom/whatsapp/driver.py. Logs ring/call_started/call_ended, relays audio to background.
//
// Selector source (selectors.py, discovered 2026-10-02, WhatsApp Web Chrome 151):
//   LOGGED_IN_MARKER   css [data-testid="chat-list"]
//   INCOMING_CALL_ROOT css [data-testid="voip-container-audio-call"], [data-testid="voip-container-incoming-video-call"]
//   INCOMING_CALLER    css [data-testid="voip-call-participant-info-name"]
//   INCOMING_IS_VIDEO  css [data-testid="voip-container-incoming-video-call"]
//   ACCEPT_BUTTON      role ^Accept$  (button aria-label="Accept")
//   DECLINE_BUTTON     role ^Decline$
//   ACTIVE_CALL_ROOT   css [data-testid="voip-container-audio-call"]
//   CAMERA_TOGGLE      role ^Turn camera on$
//   CALL_TIMER         css [data-testid="voip-call-timer"]
//   HANGUP_BUTTON      role ^End call$  (button aria-label="End call")
// Call-end rule (driver.is_call_active): End-call visible AND (no timer OR timer still
// ticking; frozen > TIMER_STALE_S=4s => ended).
(function () {
  'use strict';
  const TAG = '[PHATHOM content]';
  const log = (...a) => { try { console.log(TAG, ...a); } catch (e) {} };

  const SEL = {
    LOGGED_IN: '[data-testid="chat-list"]',
    INCOMING_ROOT: '[data-testid="voip-container-audio-call"], [data-testid="voip-container-incoming-video-call"]',
    INCOMING_CALLER: '[data-testid="voip-call-participant-info-name"]',
    INCOMING_IS_VIDEO: '[data-testid="voip-container-incoming-video-call"]',
    CALL_TIMER: '[data-testid="voip-call-timer"]',
  };
  const TIMER_STALE_S = 4.0;

  const frameInfo = { href: location.href, isTop: window === window.top };
  log('loaded in frame href=' + frameInfo.href + ' isTop=' + frameInfo.isTop);

  // --- nonce handshake with inject.js ---
  const NONCE = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));
  let WORKLET_URL = null;
  try { WORKLET_URL = chrome.runtime.getURL('worklets/pcm-tap.js'); } catch (e) {}
  function sendInit() {
    try {
      window.postMessage({ source: 'phathom', direction: 'content-to-inject', type: 'init', nonce: NONCE, workletUrl: WORKLET_URL }, '*');
    } catch (e) {}
  }
  sendInit();
  setTimeout(sendInit, 2000);

  // --- audio/event relay to background over a long-lived Port ---
  let port = null;
  let eventQueue = [];
  function connectPort() {
    try {
      port = chrome.runtime.connect({ name: 'audio' });
      port.onDisconnect.addListener(() => { port = null; setTimeout(connectPort, 1000); });
      // Flush queued events.
      const q = eventQueue; eventQueue = [];
      q.forEach((m) => { try { port.postMessage(m); } catch (e) {} });
      sendToServer({ t: 'hello', frame: frameInfo.href, isTop: frameInfo.isTop });
    } catch (e) { setTimeout(connectPort, 1000); }
  }
  connectPort();

  function sendToServer(msg) {
    // Attach frame context to every event so Phase-0 Q1 has evidence.
    try {
      const m = Object.assign({ _frame: frameInfo.href, _isTop: frameInfo.isTop }, msg);
      if (port) port.postMessage(m);
      else eventQueue.push(m);
    } catch (e) {}
  }

  function b64encode(buf) {
    try {
      const bytes = new Uint8Array(buf);
      let s = '';
      const CH = 8192;
      for (let i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
      return btoa(s);
    } catch (e) { return null; }
  }

  // Messages from inject.js (same page, structured clone incl. ArrayBuffers).
  window.addEventListener('message', (ev) => {
    try {
      const d = ev.data;
      if (!d || d.source !== 'phathom' || d.direction !== 'inject-to-content') return;
      if (d.type === 'audio') {
        let b64 = null;
        try {
          if (d.pcm instanceof ArrayBuffer) b64 = b64encode(d.pcm);
          else if (typeof d.pcm === 'string') b64 = d.pcm;
        } catch (e) {}
        if (b64) sendToServer({ t: 'audio', track: d.track || 'caller', pcmBase64: b64 });
      } else if (d.type === 'ready') {
        log('inject ready hooks=' + JSON.stringify(d.hooks) + ' worklet=' + d.worklet + ' frame=' + (d.frame && d.frame.href));
        sendToServer({ t: 'hooks', hooks: d.hooks, worklet: d.worklet });
      } else if (d.type === 'diag') {
        sendToServer({ t: 'diag', key: d.key, data: d.data });
      } else if (d.type === 'tone_started' || d.type === 'tone_ended') {
        log('test tone ' + d.type);
        sendToServer({ t: d.type === 'tone_started' ? 'tone_started' : 'tone_ended' });
      }
    } catch (e) {}
  });

  // --- DOM helpers (visible-only, mirrors driver._first_visible) ---
  function isVisible(el) {
    try {
      if (!el) return false;
      const r = el.getClientRects();
      if (!r || r.length === 0) return false;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
      return true;
    } catch (e) { return false; }
  }
  function qVisible(sel) {
    try {
      const els = document.querySelectorAll(sel);
      for (const el of els) if (isVisible(el)) return el;
    } catch (e) {}
    return null;
  }
  function roleButton(re) {
    try {
      const btns = document.querySelectorAll('button[aria-label]');
      for (const b of btns) {
        const name = b.getAttribute('aria-label') || '';
        if (re.test(name) && isVisible(b)) return b;
      }
    } catch (e) {}
    return null;
  }
  const findAccept = () => roleButton(/^Accept$/i);
  const findHangup = () => roleButton(/^End call$/i);
  const findTimer = () => qVisible(SEL.CALL_TIMER);
  const findCaller = () => qVisible(SEL.INCOMING_CALLER);

  function callerName() {
    try {
      const el = findCaller();
      if (!el) return 'Unknown';
      const t = (el.innerText || el.textContent || '').trim().replace(/^~+/, '').trim();
      return t || 'Unknown';
    } catch (e) { return 'Unknown'; }
  }

  // --- watcher state (mirrors driver poll + is_call_active) ---
  let ringing = false;
  let ringCaller = null;
  let ringIsVideo = false;
  let inCall = false;
  let hadRing = false; // ring seen in this episode (for incoming vs outgoing)
  let ringGoneAt = 0; // a ring that ended > RING_GRACE_S ago without a call was missed/declined
  const RING_GRACE_S = 5;
  let callPeer = null;
  let callDir = null;
  let timerText = null;
  let timerChangedAt = 0;
  let lastLoggedIn = null;

  function computeActive() {
    const hangup = findHangup();
    const now = performance.now() / 1000;
    if (!hangup) { timerText = null; timerChangedAt = now; return { active: false, timer: null }; }
    let timer = null;
    try { const t = findTimer(); timer = t ? (t.innerText || t.textContent || '').trim() : null; } catch (e) {}
    if (!timer) { timerChangedAt = now; return { active: true, timer: null }; }
    if (timer !== timerText) { timerText = timer; timerChangedAt = now; return { active: true, timer }; }
    return { active: (now - timerChangedAt) < TIMER_STALE_S, timer };
  }

  function checkOnce() {
    try {
      // Logged-in marker (wa_status on change).
      let loggedIn = false;
      try { loggedIn = !!qVisible(SEL.LOGGED_IN); } catch (e) {}
      if (loggedIn !== lastLoggedIn) {
        lastLoggedIn = loggedIn;
        log('wa_status logged_in=' + loggedIn);
        sendToServer({ t: 'wa_status', logged_in: loggedIn });
      }

      const accept = findAccept();
      if (accept && !ringing) {
        ringing = true; hadRing = true;
        ringCaller = callerName();
        let isVideo = false;
        try { isVideo = !!qVisible(SEL.INCOMING_IS_VIDEO); } catch (e) {}
        ringIsVideo = isVideo;
        log('PHATHOM ring caller="' + ringCaller + '" isVideo=' + isVideo);
        sendToServer({ t: 'ring', caller: ringCaller, is_video: isVideo });
      } else if (!accept && ringing) {
        ringing = false;
        ringGoneAt = performance.now() / 1000;
        log('PHATHOM ring_gone (caller was "' + ringCaller + '")');
        sendToServer({ t: 'ring_gone' });
      } else if (accept && ringing) {
        // Keep caller label fresh.
        ringCaller = callerName();
      }

      if (hadRing && !ringing && !inCall && performance.now() / 1000 - ringGoneAt > RING_GRACE_S) {
        hadRing = false; // missed/declined ring must not make the next outgoing call look incoming
      }

      const { active, timer } = computeActive();
      if (active && !inCall) {
        inCall = true;
        callPeer = callerName();
        callDir = hadRing ? 'incoming' : 'outgoing';
        // Spike never auto-answers: every connected call was answered/placed by the owner.
        log('PHATHOM call_started direction=' + callDir + ' peer="' + callPeer + '" answered_by=owner timer=' + timer);
        sendToServer({ t: 'call_started', direction: callDir, peer: callPeer, answered_by: 'owner' });
      } else if (!active && inCall) {
        inCall = false;
        log('PHATHOM call_ended peer="' + callPeer + '" dir=' + callDir);
        sendToServer({ t: 'call_ended', reason: 'ui-gone', peer: callPeer, direction: callDir });
        callPeer = null; callDir = null; hadRing = false;
      }
    } catch (e) {
      // Never throw into the page; watcher just retries next tick.
    }
  }

  const observer = new MutationObserver(() => {
    // Debounce to ~100 ms.
    if (checkOnce._t) return;
    checkOnce._t = setTimeout(() => { checkOnce._t = null; checkOnce(); }, 100);
  });
  try { observer.observe(document.documentElement || document.body, { subtree: true, childList: true, attributes: true }); }
  catch (e) { try { observer.observe(document.body, { subtree: true, childList: true }); } catch (e2) {} }
  setInterval(checkOnce, 1000);
  setTimeout(checkOnce, 500);

  // --- commands from background (popup buttons) ---
  function toInject(msg) {
    try { window.postMessage(Object.assign({ source: 'phathom', direction: 'content-to-inject', nonce: NONCE }, msg), '*'); }
    catch (e) {}
  }
  function dumpDom(label) {
    try {
      const html = document.documentElement ? document.documentElement.outerHTML : '';
      log('Dump DOM: ' + html.length + ' chars from ' + location.href);
      sendToServer({ t: 'dom_dump', label: label || 'manual', href: location.href, html });
      return html.length;
    } catch (e) { return -1; }
  }

  try {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      try {
        if (msg && msg.cmd === 'play_tone') {
          toInject({ type: 'play_tone', freq: msg.freq || 440, durationS: msg.durationS || 3 });
          sendResponse({ ok: true });
        } else if (msg && msg.cmd === 'dump_dom') {
          const n = dumpDom(msg.label || 'manual');
          sendResponse({ ok: n >= 0, chars: n });
        } else if (msg && msg.cmd === 'ping_frame') {
          sendResponse({ ok: true, href: location.href, isTop: window === window.top, ringing, inCall, peer: callPeer });
        }
      } catch (e) { try { sendResponse({ ok: false }); } catch (e2) {} }
      return true;
    });
  } catch (e) {}
})();
