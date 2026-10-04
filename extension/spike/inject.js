// inject.js — MAIN world, document_start, all_frames. Phase 0 spike (STEP 1A).
// Hooks: getUserMedia mixer (§5.3a) + test tone, RTCPeerConnection remote tap (§5.3b),
// pcm-tap worklet with ScriptProcessor fallback (§5.3c), bot playback stub (§5.3d),
// window.postMessage control surface with nonce (§5.3e).
// RULE: every hook is try/catch and falls back to original behaviour. Never break WhatsApp.
(function () {
  'use strict';
  // Test hook: export the pure (DOM-free) helpers when loaded as a module
  // (vitest). Browsers ignore this block. Declarations hoist, so this works
  // even though the functions are defined below.
  try {
    if (typeof module !== 'undefined' && module && module.exports) {
      module.exports = { makeFramer: makeFramer, meanSquareDb: meanSquareDb };
    }
  } catch (e) {}
  if (typeof window === 'undefined') return; // node/vitest: helpers only
  if (window.__phathom) return;
  window.__phathom = { version: '0.0.0-spike' };

  const log = (...a) => { try { console.log('[PHATHOM inject]', ...a); } catch (e) {} };
  const warn = (...a) => { try { console.warn('[PHATHOM inject]', ...a); } catch (e) {} };

  const state = {
    nonce: null,
    workletUrl: null,
    ctx: null,
    micGain: null,
    botGain: null,
    mixDest: null,
    mixStream: null,
    realMicStream: null,
    workletMode: 'pending', // worklet | scriptprocessor | failed
    ownerTap: null, // { node, kind }
    callerCount: 0,
    botSources: new Set(),
  };

  function ensureCtx() {
    if (state.ctx) return state.ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error('no AudioContext');
    state.ctx = new AC({ sampleRate: 48000 });
    // micGain/mixDest are created per getUserMedia call (see installGum): WhatsApp stops the
    // returned track when a call ends, and a shared destination track would stay dead for the
    // next call. botGain is shared and fans out to every live destination.
    state.botGain = state.ctx.createGain();
    state.botGain.gain.value = 1;
    // Silent sink so tap nodes are pulled by the render graph even with no audible output.
    state.tapSink = state.ctx.createGain();
    state.tapSink.gain.value = 0;
    state.tapSink.connect(state.ctx.destination);
    try { ours.add(state.tapSink); ours.add(state.botGain); } catch (e) {}
    return state.ctx;
  }

  // --- 16k framing helpers shared by worklet/scriptprocessor paths ---
  function makeFramer(track, onBatch) {
    // Collects int16 samples, emits 2880-byte (90 ms) ArrayBuffers.
    let pending = [];
    return {
      push(v) {
        let s = Math.max(-1, Math.min(1, v));
        s = s < 0 ? s * 0x8000 : s * 0x7fff;
        pending.push(s | 0);
        if (pending.length >= 1440) {
          const buf = new ArrayBuffer(2880);
          const view = new DataView(buf);
          for (let i = 0; i < 1440; i++) view.setInt16(i * 2, pending[i], true);
          pending.splice(0, 1440);
          try { onBatch(track, buf); } catch (e) {}
        }
      },
    };
  }

  function emitToContent(track, buf) {
    noteTapRms(track, buf);
    // Same-tab postMessage supports structured clone with transfer.
    try {
      window.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'audio', track, pcm: buf }, '*', [buf]);
    } catch (e) {
      try {
        window.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'audio', track, pcm: buf }, '*');
      } catch (e2) {}
    }
  }

  // --- per-tap level reporting: NON-deduped rms diag every 2 s per tap ---
  // Lets the server tell which tap carries the caller's voice (STEP 1A).
  const tapStats = {}; // label -> {sum, n} of squared full-scale-normalized samples
  function meanSquareDb(ms) {
    return ms > 0 ? 10 * Math.log10(ms) : -Infinity;
  }
  function noteTapRms(track, buf) {
    try {
      const v = new Int16Array(buf);
      if (v.length === 0) return;
      let s = 0;
      for (let i = 0; i < v.length; i++) {
        const f = v[i] / 0x8000;
        s += f * f;
      }
      const st = tapStats[track] || (tapStats[track] = { sum: 0, n: 0 });
      st.sum += s;
      st.n += v.length;
    } catch (e) {}
  }
  try {
    setInterval(() => {
      try {
        Object.keys(tapStats).forEach((label) => {
          const st = tapStats[label];
          const ms = st.n ? st.sum / st.n : 0;
          let db = meanSquareDb(ms);
          if (!isFinite(db)) db = -999;
          window.postMessage({
            source: 'phathom', direction: 'inject-to-content', type: 'diag',
            key: 'rms', data: { label, rms: +Math.sqrt(ms).toFixed(4), db: +db.toFixed(1) },
          }, '*');
          st.sum = 0;
          st.n = 0;
        });
      } catch (e) {}
    }, 2000);
  } catch (e) {}

  // --- diagnostics: each distinct key is reported to the server once ---
  const diagSeen = new Set();
  function diag(key, data) {
    try {
      if (diagSeen.has(key)) return;
      diagSeen.add(key);
      log('diag ' + key, data || '');
      window.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'diag', key, data: data || null }, '*');
    } catch (e) {}
  }

  // Nodes we create ourselves; the AudioNode.connect hook must ignore them.
  const ours = new WeakSet();
  const sinks = new WeakMap(); // ctx -> silent sink connected to ctx.destination
  const modules = new WeakMap(); // ctx -> addModule promise

  function sinkFor(ctx) {
    if (ctx === state.ctx && state.tapSink) return state.tapSink;
    let s = sinks.get(ctx);
    if (!s) {
      s = ctx.createGain();
      s.gain.value = 0;
      ours.add(s);
      s.connect(ctx.destination);
      sinks.set(ctx, s);
    }
    return s;
  }

  async function makeTapNode(track, ctxArg) {
    // Returns { node, kind } or throws. Prefers AudioWorklet, falls back to ScriptProcessor.
    // ctxArg lets us tap nodes living in WhatsApp's own AudioContexts.
    const ctx = ctxArg || ensureCtx();
    const url = state.workletUrl;
    if (url) {
      try {
        let p = modules.get(ctx);
        if (!p) { p = ctx.audioWorklet.addModule(url); modules.set(ctx, p); }
        await p;
        const node = new AudioWorkletNode(ctx, 'pcm-tap', { processorOptions: { track } });
        ours.add(node);
        node.connect(sinkFor(ctx)); // not reachable from destination => may never be processed
        node.port.onmessage = (ev) => {
          try {
            const d = ev.data || {};
            if (d.pcm) emitToContent(d.track || track, d.pcm);
          } catch (e) {}
        };
        if (state.workletMode === 'pending') {
          state.workletMode = 'worklet';
          log('worklet loaded OK (track=' + track + ')');
          reportReady();
        }
        return { node, kind: 'worklet' };
      } catch (e) {
        warn('worklet addModule failed, using ScriptProcessor fallback:', String(e && e.message || e));
      }
    }
    // ScriptProcessor fallback with identical output (linear resample to 16k, 90ms batches).
    try {
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      ours.add(proc);
      const framer = makeFramer(track, emitToContent);
      let carry = 0;
      proc.onaudioprocess = (ev) => {
        try {
          const inp = ev.inputBuffer.getChannelData(0);
          const ratio = ev.inputBuffer.sampleRate / 16000;
          let pos = carry;
          while (pos < inp.length) {
            const i0 = Math.floor(pos);
            const i1 = Math.min(inp.length - 1, i0 + 1);
            const frac = pos - i0;
            framer.push(inp[i0] * (1 - frac) + inp[i1] * frac);
            pos += ratio;
          }
          carry = pos - inp.length;
        } catch (e) {}
      };
      // ScriptProcessor must be connected (to a zero-gain sink) to run.
      proc.connect(sinkFor(ctx));
      if (state.workletMode === 'pending') {
        state.workletMode = 'scriptprocessor';
        log('ScriptProcessor fallback active (track=' + track + ')');
        reportReady();
      }
      return { node: proc, kind: 'scriptprocessor' };
    } catch (e) {
      if (state.workletMode === 'pending') {
        state.workletMode = 'failed';
        reportReady();
      }
      throw e;
    }
  }

  function reportReady() {
    try {
      window.postMessage({
        source: 'phathom', direction: 'inject-to-content', type: 'ready',
        hooks: { gum: !!hooksOk.gum, pc: !!hooksOk.pc },
        worklet: state.workletMode,
        frame: { href: location.href, isTop: window === window.top },
      }, '*');
    } catch (e) {}
  }

  const hooksOk = { gum: false, pc: false };

  // --- a) Mic mixer: wrap getUserMedia ---
  function installGum() {
    try {
      const md = navigator.mediaDevices;
      if (!md || !md.getUserMedia) { warn('no mediaDevices.getUserMedia to wrap'); return; }
      const orig = md.getUserMedia.bind(md);
      md.getUserMedia = async function (constraints) {
        try {
          const stream = await orig(constraints);
          try {
            const wantsAudio = !!(constraints && constraints.audio);
            if (!wantsAudio) return stream;
            const ctx = ensureCtx();
            if (ctx.state === 'suspended') { ctx.resume().catch(() => {}); }
            const audioTracks = stream.getAudioTracks();
            if (!audioTracks || audioTracks.length === 0) return stream;
            state.realMicStream = stream;
            // Fresh graph per stream: realMic -> micGain -> dest (+ shared botGain -> dest).
            const src = ctx.createMediaStreamSource(stream);
            const micGain = ctx.createGain();
            micGain.gain.value = 1;
            const dest = ctx.createMediaStreamDestination();
            src.connect(micGain);
            micGain.connect(dest);
            state.botGain.connect(dest);
            state.micGain = micGain; // set_gain targets the current call's mic
            state.mixDest = dest;
            // Owner tap observes the mic (post micGain so micGain=0 mutes it too).
            let tap = null;
            try {
              tap = await makeTapNode('owner');
              state.ownerTap = tap;
              micGain.connect(tap.node);
            } catch (e) { warn('owner tap failed, mic still passes through:', e); }
            let torn = false;
            const teardown = () => {
              if (torn) return;
              torn = true;
              try { src.disconnect(); } catch (e) {}
              try { micGain.disconnect(); } catch (e) {}
              try { state.botGain.disconnect(dest); } catch (e) {}
              try { if (tap) tap.node.disconnect(); } catch (e) {}
              log('mixer stream torn down');
            };
            const videoTracks = stream.getVideoTracks ? stream.getVideoTracks() : [];
            const out = new MediaStream();
            dest.stream.getAudioTracks().forEach((t) => {
              out.addTrack(t);
              // Forward stop() to the real mic.
              const origStop = t.stop.bind(t);
              t.stop = function () {
                try { origStop(); } catch (e) {}
                try { audioTracks.forEach((rt) => rt.stop()); } catch (e) {}
                teardown();
              };
            });
            videoTracks.forEach((t) => out.addTrack(t));
            state.mixStream = out;
            log('getUserMedia wrapped: returning mixed stream (mic->mixer, owner tap on)');
            return out;
          } catch (e) {
            warn('mixer setup failed, returning original stream:', e);
            return stream;
          }
        } catch (e) {
          throw e; // original getUserMedia itself failed; propagate
        }
      };
      // Legacy navigator.getUserMedia fallback.
      try {
        if (navigator.getUserMedia && !navigator.getUserMedia.__phathom) {
          const oget = navigator.getUserMedia.bind(navigator);
          const wrapped = function (c, ok, err) {
            try { return md.getUserMedia(c).then(ok, err); }
            catch (e) { try { return oget(c, ok, err); } catch (e2) { if (err) err(e2); } }
          };
          wrapped.__phathom = true;
          navigator.getUserMedia = wrapped;
        }
      } catch (e) {}
      hooksOk.gum = true;
      log('getUserMedia hook installed');
    } catch (e) {
      warn('installGum failed:', e);
    }
  }

  // --- b) Remote audio tap: wrap RTCPeerConnection ---
  function installPc() {
    try {
      const Orig = window.RTCPeerConnection;
      if (!Orig || Orig.__phathom) { if (Orig && Orig.__phathom) hooksOk.pc = true; return; }
      class PhathomPC extends Orig {
        constructor(...args) {
          super(...args);
          try { hookPcInstance(this); } catch (e) {}
        }
      }
      // Preserve statics + name so WhatsApp feature-detects normally.
      try {
        Object.getOwnPropertyNames(Orig).forEach((k) => {
          if (k === 'prototype' || k === 'name' || k === 'length') return;
          try { PhathomPC[k] = Orig[k]; } catch (e) {}
        });
      } catch (e) {}
      try { Object.defineProperty(PhathomPC, 'name', { value: 'RTCPeerConnection' }); } catch (e) {}
      PhathomPC.__phathom = true;
      PhathomPC.__orig = Orig;
      window.RTCPeerConnection = PhathomPC;
      hooksOk.pc = true;
      log('RTCPeerConnection hook installed');
    } catch (e) {
      warn('installPc failed:', e);
    }
  }

  function hookPcInstance(pc) {
    diag('pc_created');
    const onTrack = (ev) => {
      try {
        diag('pc_track_' + (ev && ev.track && ev.track.kind));
        const streams = (ev && ev.streams) || [];
        const track = ev && ev.track;
        if (!track || track.kind !== 'audio') return;
        state.callerCount += 1;
        const idx = state.callerCount;
        const label = idx === 1 ? 'caller' : ('caller:' + idx);
        if (idx > 1) warn('extra remote audio track seen (group call?); recording only caller:1, got ' + label);
        const ms = (streams && streams[0]) ? streams[0] : new MediaStream([track]);
        tapRemoteStream(ms, label);
      } catch (e) {}
    };
    // addEventListener fires regardless of how WhatsApp sets pc.ontrack; leave ontrack untouched.
    try { pc.addEventListener('track', onTrack); } catch (e) {}
    try {
      pc.addEventListener('connectionstatechange', () => {
        diag('pc_state_' + pc.connectionState);
        if (pc.connectionState === 'closed' || pc.connectionState === 'failed') state.callerCount = 0;
        if (pc.connectionState === 'connected') {
          // Backup for stacks that never fire 'track': read the receivers directly.
          try {
            const kinds = pc.getReceivers().map((r) => r.track && r.track.kind);
            diag('pc_receivers', kinds);
            if (state.callerCount === 0) {
              pc.getReceivers().forEach((r) => {
                if (r.track && r.track.kind === 'audio') {
                  state.callerCount += 1;
                  tapRemoteStream(new MediaStream([r.track]), 'caller');
                }
              });
            }
          } catch (e) {}
        }
      });
    } catch (e) {}
  }

  // --- f) Speaker taps: EVERYTHING WhatsApp plays out through Web Audio ---
  // If WhatsApp decodes call audio itself (WASM/worklets) instead of using a WebRTC
  // audio track, the caller's voice still has to reach an AudioContext destination.
  // Tap every non-ours node that connects to ANY AudioDestinationNode, one tap per
  // node (deduped per node, NOT per type): speaker:1, speaker:2, ...
  const tappedNodes = new WeakSet();
  function installSpeakerTap() {
    try {
      const proto = window.AudioNode && window.AudioNode.prototype;
      if (!proto || proto.connect.__phathom) return;
      const origConnect = proto.connect;
      const wrapped = function (dest, ...rest) {
        const result = origConnect.call(this, dest, ...rest);
        try {
          if (dest instanceof AudioDestinationNode && !ours.has(this)
              && !tappedNodes.has(this)) {
            tappedNodes.add(this);
            const ctx = this.context;
            const cid = ctxId(ctx);
            const name = (this.constructor && this.constructor.name) || '?';
            state.speakerN = (state.speakerN || 0) + 1;
            const label = 'speaker:' + state.speakerN + '(' + name + ')';
            diag('dest_connect_' + name + '_' + ctx.sampleRate + '_' + cid + '_' + state.speakerN,
                 { node: name, rate: ctx.sampleRate, ctx: cid, label });
            const node = this;
            makeTapNode(label, ctx).then((tap) => {
              try { origConnect.call(node, tap.node); diag('tap_on_' + label + '_' + tap.kind); } catch (e) {}
            }).catch((e) => diag('tap_failed_' + label, String(e && e.message || e)));
          }
        } catch (e) {}
        return result;
      };
      wrapped.__phathom = true;
      proto.connect = wrapped;
      log('AudioNode.connect hook installed');
    } catch (e) {
      warn('installSpeakerTap failed:', e);
    }
  }

  // Wrap a native constructor so subclasses keep working: WhatsApp may do
  // `class X extends AudioWorkletNode`, and a wrapper that returns `new Orig()` would hand it
  // an object without X's prototype (its methods missing). Reflect.construct with new.target
  // builds the right object; setPrototypeOf keeps static members.
  function wrapCtor(Orig, name, onCreate) {
    const W = function (...args) {
      if (!new.target) return Orig.apply(this, args); // called without new: let native throw
      const obj = Reflect.construct(Orig, args, new.target === W ? Orig : new.target);
      try { onCreate(obj, args); } catch (e) {}
      return obj;
    };
    W.prototype = Orig.prototype;
    try { Object.setPrototypeOf(W, Orig); } catch (e) {}
    try { Object.defineProperty(W, 'name', { value: name }); } catch (e) {}
    W.__phathom = true;
    return W;
  }

  // --- g) AudioContext construction: who creates contexts, at what rate ---
  const ctxIds = new WeakMap();
  let ctxSeq = 0;
  function ctxId(ctx) {
    try {
      if (!ctx || !ctxIds.has(ctx)) {
        ctxSeq += 1;
        if (ctx) ctxIds.set(ctx, ctxSeq);
        return ctxSeq;
      }
      return ctxIds.get(ctx);
    } catch (e) { return 0; }
  }
  function installCtxHook() {
    try {
      const Orig = window.AudioContext;
      if (!Orig || Orig.__phathom) return;
      window.AudioContext = wrapCtor(Orig, 'AudioContext', (c, args) => {
        let hint = '';
        try { hint = (args[0] && args[0].latencyHint) || ''; } catch (e) {}
        diag('ctx_' + ctxId(c) + '_' + c.sampleRate, { rate: c.sampleRate, hint: String(hint) });
      });
      log('AudioContext hook installed');
    } catch (e) {
      warn('installCtxHook failed:', e);
    }
    // Output-device selection (Chrome 110+): which sink does WhatsApp pick?
    try {
      const P = window.AudioContext && window.AudioContext.prototype;
      if (P && P.setSinkId && !P.setSinkId.__phathom) {
        const orig = P.setSinkId;
        const w = function (deviceId, ...rest) {
          try { diag('setsinkid_ctx' + ctxId(this) + '_' + String(deviceId).slice(0, 60)); } catch (e) {}
          return orig.call(this, deviceId, ...rest);
        };
        w.__phathom = true;
        P.setSinkId = w;
      }
    } catch (e) {}
  }

  // --- h) AudioWorkletNode construction: processor names on each context ---
  function installWorkletNodeHook() {
    try {
      const Orig = window.AudioWorkletNode;
      if (!Orig || Orig.__phathom) return;
      window.AudioWorkletNode = wrapCtor(Orig, 'AudioWorkletNode', (node, args) => {
        diag('workletnode_' + String(args[1]).slice(0, 60) + '_ctx' + ctxId(args[0])
             + '_io' + node.numberOfInputs + 'x' + node.numberOfOutputs);
      });
      log('AudioWorkletNode hook installed');
    } catch (e) {
      warn('installWorkletNodeHook failed:', e);
    }
  }

  // --- i) Media-element playout: play() with src/srcObject kind + sink ---
  function installMediaPlayHook() {
    try {
      const P = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
      if (!P || P.play.__phathom) return;
      const orig = P.play;
      const w = function (...args) {
        try {
          let kind = 'none';
          try {
            const s = this.srcObject;
            kind = s ? ('stream:' + ((s.getTracks && s.getTracks().map((t) => t.kind).join(',')) || '?'))
                     : ('attr:' + String(this.currentSrc || this.src || 'empty').slice(-80));
          } catch (e) {}
          let sink = '';
          try { sink = this.sinkId || ''; } catch (e) {}
          diag('media_play_' + kind + '_sink_' + String(sink).slice(0, 40));
        } catch (e) {}
        return orig.apply(this, args);
      };
      w.__phathom = true;
      P.play = w;
      log('HTMLMediaElement.play hook installed');
    } catch (e) {
      warn('installMediaPlayHook failed:', e);
    }
    // Stream destinations created for call routing (diagnostic: who creates them).
    try {
      const P = window.AudioContext && window.AudioContext.prototype;
      if (P && P.createMediaStreamDestination && !P.createMediaStreamDestination.__phathom) {
        const orig = P.createMediaStreamDestination;
        const w = function (...args) {
          try { diag('createmediadest_ctx' + ctxId(this)); } catch (e) {}
          return orig.apply(this, args);
        };
        w.__phathom = true;
        P.createMediaStreamDestination = w;
      }
    } catch (e) {}
  }

  // Logging only: how does WhatsApp play and process audio?
  function installProbes() {
    try {
      const AW = window.AudioWorklet && window.AudioWorklet.prototype;
      if (AW && !AW.addModule.__phathom) {
        const orig = AW.addModule;
        AW.addModule = function (url, ...rest) {
          try { if (String(url) !== state.workletUrl) diag('worklet_module_' + String(url).slice(0, 120)); } catch (e) {}
          return orig.call(this, url, ...rest);
        };
        AW.addModule.__phathom = true;
      }
    } catch (e) {}
    try {
      const OrigWorker = window.Worker;
      if (OrigWorker && !OrigWorker.__phathom) {
        window.Worker = wrapCtor(OrigWorker, 'Worker', (w, args) => {
          diag('worker_' + String(args[0]).slice(0, 120));
        });
      }
    } catch (e) {}
    try {
      const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
      if (desc && desc.set && !desc.set.__phathom) {
        const set = function (v) {
          try {
            const kinds = v && v.getTracks ? v.getTracks().map((t) => t.kind).join(',') : typeof v;
            diag('media_srcObject_' + kinds);
          } catch (e) {}
          return desc.set.call(this, v);
        };
        set.__phathom = true;
        Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', { ...desc, set });
      }
    } catch (e) {}
  }

  async function tapRemoteStream(mediaStream, label) {
    try {
      const ctx = ensureCtx();
      if (ctx.state === 'suspended') { ctx.resume().catch(() => {}); }
      const src = ctx.createMediaStreamSource(mediaStream);
      const tap = await makeTapNode(label);
      src.connect(tap.node);
      // ⚠️ Chromium can feed silence to WebAudio unless the stream is also on a media element.
      try {
        const a = new Audio();
        a.muted = true;
        a.srcObject = mediaStream;
        const p = a.play();
        if (p && p.catch) p.catch(() => {});
      } catch (e) {}
      log('remote tap on (' + label + ') via ' + tap.kind);
    } catch (e) {
      warn('tapRemoteStream failed (' + label + '), call continues untapped:', e);
    }
  }

  // --- d) Bot playback: test tone through the mixer (bot path) ---
  function playTestTone(freq, durationS) {
    freq = freq || 440;
    durationS = durationS || 3;
    try {
      const ctx = ensureCtx();
      if (ctx.state === 'suspended') { ctx.resume().catch(() => {}); }
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const g = ctx.createGain();
      const t = ctx.currentTime;
      // Short fades to avoid clicks.
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.5, t + 0.05);
      g.gain.setValueAtTime(0.5, t + durationS - 0.05);
      g.gain.exponentialRampToValueAtTime(0.0001, t + durationS);
      osc.connect(g);
      g.connect(state.botGain);
      state.botSources.add(osc);
      osc.onended = () => { try { state.botSources.delete(osc); } catch (e) {} try { g.disconnect(); } catch (e) {} };
      osc.start(t);
      osc.stop(t + durationS + 0.05);
      log('playing test tone ' + freq + 'Hz ' + durationS + 's through bot path');
      window.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'tone_started', freq, durationS }, '*');
      osc.onended = (() => {
        const prev = osc.onended;
        return () => {
          try { if (prev) prev(); } catch (e) {}
          try {
            window.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'tone_ended', freq, durationS }, '*');
          } catch (e) {}
        };
      })();
      return true;
    } catch (e) {
      warn('playTestTone failed:', e);
      return false;
    }
  }

  function stopAllBotAudio() {
    try {
      state.botSources.forEach((s) => { try { s.stop(); } catch (e) {} });
      state.botSources.clear();
    } catch (e) {}
  }

  // --- e) Control surface (content.js <-> inject.js), nonce-gated ---
  window.addEventListener('message', (ev) => {
    try {
      const d = ev.data;
      if (!d || d.source !== 'phathom' || d.direction !== 'content-to-inject') return;
      if (d.type === 'init') {
        try {
          if (!state.nonce) {
            state.nonce = d.nonce;
            state.workletUrl = d.workletUrl || null;
            log('init: nonce set, workletUrl=' + state.workletUrl);
          }
          reportReady();
        } catch (e) {}
        return;
      }
      if (d.nonce !== state.nonce) return; // ignore spoofed commands
      if (d.type === 'play_tone') {
        playTestTone(d.freq || 440, d.durationS || 3);
      } else if (d.type === 'stop_audio') {
        stopAllBotAudio();
      } else if (d.type === 'set_gain') {
        try {
          if (state.ctx) {
            if (typeof d.mic === 'number' && state.micGain) state.micGain.gain.value = d.mic;
            if (typeof d.bot === 'number') state.botGain.gain.value = d.bot;
            log('gains set mic=' + (state.micGain ? state.micGain.gain.value : 'n/a') + ' bot=' + state.botGain.gain.value);
          }
        } catch (e) {}
      }
    } catch (e) {}
  });

  // Install immediately (document_start, before WhatsApp grabs references).
  // Order matters: ctx/worklet hooks first so WhatsApp's own setup is observed.
  installCtxHook();
  installWorkletNodeHook();
  installGum();
  installPc();
  installSpeakerTap();
  installMediaPlayHook();
  installProbes();
  // Report frame info once the DOM exists too.
  try {
    document.addEventListener('DOMContentLoaded', () => { try { reportReady(); } catch (e) {} });
  } catch (e) {}
})();
