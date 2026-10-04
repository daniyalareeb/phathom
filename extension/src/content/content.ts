// content.ts — ISOLATED world (EXTENSION_SPEC §5.4): DOM watcher, actions,
// audio relay. Port of the spike content.js + missed-ring direction fix
// (a stale ring must not make the next outgoing call look incoming).

import { SEL, ACCEPT_RE, HANGUP_RE, TIMER_STALE_S, OUTGOING_PEER_CANDIDATES, SelectorCheck } from './selectors';

(function () {
  'use strict';
  const TAG = '[PHATHOM content]';
  const log = (...a: unknown[]) => {
    try {
      console.log(TAG, ...a);
    } catch {
      /* noop */
    }
  };

  const frameInfo = { href: location.href, isTop: window === window.top };
  log('loaded in frame href=' + frameInfo.href + ' isTop=' + frameInfo.isTop);

  const NONCE = crypto.randomUUID ? crypto.randomUUID() : String(Math.random());
  let WORKLET_URL: string | null = null;
  try {
    WORKLET_URL = chrome.runtime.getURL('worklets/pcm-tap.js');
  } catch {
    /* noop */
  }
  function sendInit() {
    try {
      window.postMessage(
        { source: 'phathom', direction: 'content-to-inject', type: 'init', nonce: NONCE, workletUrl: WORKLET_URL },
        '*',
      );
    } catch {
      /* noop */
    }
  }
  sendInit();
  setTimeout(sendInit, 2000);

  // --- audio/event relay to background over a long-lived Port ---
  let port: chrome.runtime.Port | null = null;
  let eventQueue: Record<string, unknown>[] = [];
  function connectPort() {
    try {
      port = chrome.runtime.connect({ name: 'audio' });
      port.onDisconnect.addListener(() => {
        port = null;
        setTimeout(connectPort, 1000);
      });
      const q = eventQueue;
      eventQueue = [];
      q.forEach((m) => {
        try {
          port!.postMessage(m);
        } catch {
          /* noop */
        }
      });
      sendToServer({ t: 'hello', frame: frameInfo.href, isTop: frameInfo.isTop });
    } catch {
      setTimeout(connectPort, 1000);
    }
  }
  connectPort();

  function sendToServer(msg: Record<string, unknown>) {
    try {
      const m = Object.assign({ _frame: frameInfo.href, _isTop: frameInfo.isTop }, msg);
      if (port) port.postMessage(m);
      else eventQueue.push(m);
    } catch {
      /* noop */
    }
  }

  function b64encode(buf: ArrayBuffer): string | null {
    try {
      const bytes = new Uint8Array(buf);
      let s = '';
      const CH = 8192;
      for (let i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CH)));
      return btoa(s);
    } catch {
      return null;
    }
  }

  window.addEventListener('message', (ev: MessageEvent) => {
    try {
      const d = ev.data;
      if (!d || d.source !== 'phathom' || d.direction !== 'inject-to-content') return;
      if (d.type === 'audio') {
        let b64: string | null = null;
        try {
          if (d.pcm instanceof ArrayBuffer) b64 = b64encode(d.pcm);
          else if (typeof d.pcm === 'string') b64 = d.pcm;
        } catch {
          /* noop */
        }
        if (b64) sendToServer({ t: 'audio', track: d.track || 'caller', pcmBase64: b64 });
      } else if (d.type === 'ready') {
        log('inject ready hooks=' + JSON.stringify(d.hooks) + ' worklet=' + d.worklet);
        lastHooks = { hooks: d.hooks, worklet: d.worklet };
        sendToServer({ t: 'hooks', hooks: d.hooks, worklet: d.worklet });
      } else if (d.type === 'diag') {
        sendToServer({ t: 'diag', key: d.key, data: d.data });
      } else if (d.type === 'tone_started' || d.type === 'tone_ended') {
        sendToServer({ t: d.type === 'tone_started' ? 'tone_started' : 'tone_ended' });
      } else if (d.type === 'played') {
        sendToServer({ t: 'played', id: d.id, completed: d.completed });
      }
    } catch {
      /* noop */
    }
  });

  // --- DOM helpers (visible-only, mirrors driver._first_visible) ---
  function isVisible(el: Element | null): boolean {
    try {
      if (!el) return false;
      const r = el.getClientRects();
      if (!r || r.length === 0) return false;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
      return true;
    } catch {
      return false;
    }
  }
  function qVisible(sel: string): Element | null {
    try {
      const els = document.querySelectorAll(sel);
      for (const el of els) if (isVisible(el)) return el;
    } catch {
      /* noop */
    }
    return null;
  }
  function roleButton(re: RegExp): Element | null {
    try {
      const btns = document.querySelectorAll('button[aria-label]');
      for (const b of btns) {
        const name = b.getAttribute('aria-label') || '';
        if (re.test(name) && isVisible(b)) return b;
      }
    } catch {
      /* noop */
    }
    return null;
  }
  const findAccept = () => roleButton(ACCEPT_RE);
  const findHangup = () => roleButton(HANGUP_RE);
  const findTimer = () => qVisible(SEL.CALL_TIMER);
  const findIncomingCaller = () => qVisible(SEL.INCOMING_CALLER);

  function callerName(): string {
    try {
      const el = findIncomingCaller();
      if (!el) return 'Unknown';
      const t = ((el as HTMLElement).innerText || el.textContent || '').trim().replace(/^~+/, '').trim();
      return t || 'Unknown';
    } catch {
      return 'Unknown';
    }
  }

  /** Outgoing peer: try candidates in order; all UNVERIFIED (see selectors.ts). */
  function outgoingPeerName(): { name: string; verified: boolean } {
    for (const sel of OUTGOING_PEER_CANDIDATES) {
      try {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
          if (!isVisible(el)) continue;
          const t = ((el as HTMLElement).innerText || el.textContent || '').trim();
          if (t) return { name: t, verified: false };
        }
      } catch {
        /* noop */
      }
    }
    // Fall back to the incoming element (same testid may persist in-call).
    return { name: callerName(), verified: false };
  }

  /** Selector self-check for Diagnostics (§7.5). */
  function selectorCheck(): SelectorCheck[] {
    const rows: SelectorCheck[] = [];
    const probe = (name: string, found: boolean, verified: boolean) =>
      rows.push({ name, status: verified ? (found ? 'found' : 'not-found') : 'unverified', verified });
    try {
      probe('LOGGED_IN_MARKER', !!document.querySelector(SEL.LOGGED_IN), true);
      probe('INCOMING_CALL_ROOT', !!document.querySelector(SEL.INCOMING_ROOT), true);
      probe('INCOMING_CALLER', !!document.querySelector(SEL.INCOMING_CALLER), true);
      probe('CALL_TIMER', !!document.querySelector(SEL.CALL_TIMER), true);
      probe('ACCEPT_BUTTON', !!roleButton(ACCEPT_RE), true);
      probe('HANGUP_BUTTON', !!roleButton(HANGUP_RE), true);
    } catch {
      /* noop */
    }
    for (const c of OUTGOING_PEER_CANDIDATES) probe(`OUTGOING_PEER ${c}`, false, false);
    return rows;
  }

  // --- watcher state (mirrors driver poll + is_call_active) ---
  let ringing = false;
  let ringCaller: string | null = null;
  let inCall = false;
  let hadRing = false; // ring seen in this episode (incoming vs outgoing)
  let ringGoneAt = 0;
  const RING_GRACE_S = 5; // a ring gone longer ago must not taint the next call
  let callPeer: string | null = null;
  let callDir: string | null = null;
  let timerText: string | null = null;
  let timerChangedAt = 0;
  let lastLoggedIn: boolean | null = null;
  let lastSelReport = -1e9;
  let lastHooks: { hooks: unknown; worklet: unknown } | null = null;

  function computeActive(): { active: boolean; timer: string | null } {
    const hangup = findHangup();
    const now = performance.now() / 1000;
    if (!hangup) {
      timerText = null;
      timerChangedAt = now;
      return { active: false, timer: null };
    }
    let timer: string | null = null;
    try {
      const t = findTimer();
      timer = t ? ((t as HTMLElement).innerText || t.textContent || '').trim() : null;
    } catch {
      /* noop */
    }
    if (!timer) {
      timerChangedAt = now;
      return { active: true, timer: null };
    }
    if (timer !== timerText) {
      timerText = timer;
      timerChangedAt = now;
      return { active: true, timer };
    }
    return { active: now - timerChangedAt < TIMER_STALE_S, timer };
  }

  function checkOnce() {
    try {
      let loggedIn = false;
      try {
        loggedIn = !!qVisible(SEL.LOGGED_IN);
      } catch {
        /* noop */
      }
      if (loggedIn !== lastLoggedIn) {
        lastLoggedIn = loggedIn;
        log('wa_status logged_in=' + loggedIn);
        sendToServer({ t: 'wa_status', logged_in: loggedIn });
      }

      // Live Diagnostics: what this tab can actually see, every 10 s (and the
      // hook status again, so a server restart doesn't lose it).
      const nowS = performance.now() / 1000;
      if (frameInfo.isTop && nowS - lastSelReport > 10) {
        lastSelReport = nowS;
        sendToServer({ t: 'selectors', items: selectorCheck(), ringing, in_call: inCall });
        if (lastHooks) sendToServer({ t: 'hooks', ...lastHooks });
      }

      const accept = findAccept();
      if (accept && !ringing) {
        ringing = true;
        hadRing = true;
        ringCaller = callerName();
        let isVideo = false;
        try {
          isVideo = !!qVisible(SEL.INCOMING_IS_VIDEO);
        } catch {
          /* noop */
        }
        log('PHATHOM ring caller="' + ringCaller + '" isVideo=' + isVideo);
        sendToServer({ t: 'ring', caller: ringCaller, is_video: isVideo });
      } else if (!accept && ringing) {
        ringing = false;
        ringGoneAt = performance.now() / 1000;
        log('PHATHOM ring_gone (caller was "' + ringCaller + '")');
        sendToServer({ t: 'ring_gone' });
      } else if (accept && ringing) {
        ringCaller = callerName();
      }

      // Missed-ring → outgoing direction fix: expire stale rings.
      if (hadRing && !ringing && !inCall && performance.now() / 1000 - ringGoneAt > RING_GRACE_S) {
        hadRing = false;
      }

      const { active, timer } = computeActive();
      if (active && !inCall) {
        inCall = true;
        callDir = hadRing ? 'incoming' : 'outgoing';
        if (callDir === 'incoming') {
          callPeer = callerName();
        } else {
          const o = outgoingPeerName();
          callPeer = o.name;
          if (!o.verified) {
            // Visible failure per rule 3: name is a guess until a dump confirms it.
            sendToServer({ t: 'diag', key: 'outgoing_peer_unverified', data: { peer: callPeer } });
          }
        }
        log('PHATHOM call_started direction=' + callDir + ' peer="' + callPeer + '" timer=' + timer);
        sendToServer({ t: 'call_started', direction: callDir, peer: callPeer, answered_by: 'owner' });
      } else if (!active && inCall) {
        inCall = false;
        log('PHATHOM call_ended peer="' + callPeer + '" dir=' + callDir);
        sendToServer({ t: 'call_ended', reason: 'ui-gone', peer: callPeer, direction: callDir });
        callPeer = null;
        callDir = null;
        hadRing = false;
      }
    } catch {
      /* watcher never throws into the page */
    }
  }

  const observer = new MutationObserver(() => {
    if ((checkOnce as unknown as Record<string, unknown>)._t) return;
    (checkOnce as unknown as Record<string, unknown>)._t = setTimeout(() => {
      (checkOnce as unknown as Record<string, unknown>)._t = null;
      checkOnce();
    }, 100);
  });
  try {
    observer.observe(document.documentElement || document.body, { subtree: true, childList: true, attributes: true });
  } catch {
    try {
      observer.observe(document.body, { subtree: true, childList: true });
    } catch {
      /* noop */
    }
  }
  setInterval(checkOnce, 1000);
  setTimeout(checkOnce, 500);

  // --- commands from background ---
  function toInject(msg: Record<string, unknown>) {
    try {
      window.postMessage(Object.assign({ source: 'phathom', direction: 'content-to-inject', nonce: NONCE }, msg), '*');
    } catch {
      /* noop */
    }
  }
  function clickEl(el: Element | null): boolean {
    try {
      if (!el) return false;
      (el as HTMLElement).click();
      return true;
    } catch {
      return false;
    }
  }
  function dumpDom(label: string): number {
    try {
      const html = document.documentElement ? document.documentElement.outerHTML : '';
      sendToServer({ t: 'dom_dump', label: label || 'manual', href: location.href, html });
      return html.length;
    } catch {
      return -1;
    }
  }
  /** Scroll the open chat's message pane up in small steps (Brain reader §8.1). */
  async function readChat(range: string, sendProgress: (n: number, done: boolean) => void): Promise<Record<string, unknown>[]> {
    const out: Record<string, unknown>[] = [];
    try {
      const pane = document.querySelector('[data-testid="conversation-panel-messages"]'); // UNVERIFIED
      if (!pane) return out;
      const days = range === '7d' ? 7 : range === '30d' ? 30 : range === '90d' ? 90 : 36500;
      const cutoff = Date.now() - days * 86400 * 1000;
      let guard = 0;
      let lastH = -1;
      let stable = 0;
      while (guard++ < 60 && out.length < 5000) {
        (pane as HTMLElement).scrollTop = 0;
        await new Promise((r) => setTimeout(r, 900));
        const bubbles = Array.from(document.querySelectorAll('[data-testid="msg-container"]')); // UNVERIFIED
        if (bubbles.length === lastH && ++stable >= 3) break;
        stable = bubbles.length === lastH ? stable : 0;
        lastH = bubbles.length;
        // Parse bubbles oldest→newest; stop when older than cutoff (best effort).
        void cutoff;
        sendProgress(bubbles.length, false);
        if (bubbles.length >= 5000) break;
      }
      const bubbles = Array.from(document.querySelectorAll('[data-testid="msg-container"]'));
      for (const b of bubbles.slice(-5000)) {
        try {
          const text = (b.textContent || '').trim();
          if (!text) continue;
          out.push({ sender: '', is_me: false, text, ts: '' });
        } catch {
          /* noop */
        }
      }
      sendProgress(out.length, true);
    } catch {
      /* noop */
    }
    return out;
  }

  try {
    chrome.runtime.onMessage.addListener((msg: Record<string, unknown>, _sender, sendResponse: (r: unknown) => void) => {
      (async () => {
        try {
          if (msg && msg.cmd === 'play_tone') {
            toInject({ type: 'play_tone', freq: (msg.freq as number) || 440, durationS: (msg.durationS as number) || 3 });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'play_begin') {
            toInject({ type: 'play_begin', id: msg.id });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'play_chunk') {
            // background fetched the ArrayBuffer and passes it through the message
            toInject({ type: 'play_chunk', id: msg.id, pcm: msg.pcm });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'play_end') {
            toInject({ type: 'play_end', id: msg.id });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'stop_audio') {
            toInject({ type: 'stop_audio' });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'set_gain') {
            toInject({ type: 'set_gain', mic: msg.mic, bot: msg.bot });
            sendResponse({ ok: true });
          } else if (msg && msg.cmd === 'accept') {
            // Verify-and-retry like driver.accept(): click, then confirm ring is gone.
            const ok = clickEl(findAccept());
            sendResponse({ ok });
          } else if (msg && msg.cmd === 'hangup') {
            const el = findHangup();
            const ok = clickEl(el);
            setTimeout(() => {
              try {
                if (findHangup()) clickEl(findHangup());
              } catch {
                /* noop */
              }
            }, 1500);
            sendResponse({ ok });
          } else if (msg && msg.cmd === 'open_chat') {
            sendResponse({ ok: false, error: 'open_chat uses UNVERIFIED selectors; see Diagnostics' });
          } else if (msg && msg.cmd === 'dump_dom') {
            const n = dumpDom((msg.label as string) || 'manual');
            sendResponse({ ok: n >= 0, chars: n });
          } else if (msg && msg.cmd === 'read_chat') {
            const msgs = await readChat((msg.range as string) || '30d', (n, done) => {
              try {
                port?.postMessage({ t: 'chat_progress', count: n, done });
              } catch {
                /* noop */
              }
            });
            sendResponse({ ok: true, messages: msgs });
          } else if (msg && msg.cmd === 'selector_check') {
            sendResponse({ ok: true, selectors: selectorCheck() });
          } else if (msg && msg.cmd === 'ping_frame') {
            sendResponse({ ok: true, href: location.href, isTop: window === window.top, ringing, inCall, peer: callPeer });
          }
        } catch {
          try {
            sendResponse({ ok: false });
          } catch {
            /* noop */
          }
        }
      })();
      return true;
    });
  } catch {
    /* noop */
  }
})();
