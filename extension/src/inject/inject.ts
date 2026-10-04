// inject.ts — MAIN world, document_start, all_frames (EXTENSION_SPEC §5.3, §5.7).
// Port of the Phase-0 spike inject.js with the reviewer's fixes intact:
//  - a fresh mixer graph per getUserMedia call (a shared destination dies with the call);
//  - tap nodes connected to a silent sink (un-pulled nodes never process audio);
//  - every constructor hook via wrapCtor() (Reflect.construct + new.target);
//  - no pc.ontrack override (addEventListener only);
//  - per-node speaker taps (deduped per node, NOT per type);
//  - all hooks try/catch with fallback to the original: WhatsApp keeps working.
// Bot playback (§5.3d): gap-free 24 kHz PCM scheduling + stop_audio + played acks.

import { makeFramer, batchLevel } from './audio';

(function () {
  'use strict';
  if (typeof window === 'undefined') return; // node/vitest: pure helpers only
  const w = window as any;
  if (w.__phathom) return;
  w.__phathom = { version: '0.2.0' };

  const log = (...a: unknown[]) => {
    try {
      console.log('[PHATHOM inject]', ...a);
    } catch {
      /* noop */
    }
  };
  const warn = (...a: unknown[]) => {
    try {
      console.warn('[PHATHOM inject]', ...a);
    } catch {
      /* noop */
    }
  };

  interface TapRef {
    node: AudioNode;
    kind: string;
  }

  const state: {
    nonce: string | null;
    workletUrl: string | null;
    ctx: AudioContext | null;
    micGain: GainNode | null;
    botGain: GainNode | null;
    tapSink: GainNode | null;
    workletMode: string;
    callerCount: number;
    speakerN: number;
    botSources: Set<AudioScheduledSourceNode>;
    playSeq: Map<number, AudioBufferSourceNode[]>;
  } = {
    nonce: null,
    workletUrl: null,
    ctx: null,
    micGain: null,
    botGain: null,
    tapSink: null,
    workletMode: 'pending',
    callerCount: 0,
    speakerN: 0,
    botSources: new Set(),
    playSeq: new Map(),
  };

  const hooksOk: Record<string, boolean> = { gum: false, pc: false };

  function ensureCtx(): AudioContext {
    const existing = state.ctx;
    if (existing) return existing;
    const AC = w.AudioContext || w.webkitAudioContext;
    if (!AC) throw new Error('no AudioContext');
    const created: AudioContext = new AC({ sampleRate: 48000 });
    state.ctx = created;
    state.botGain = created.createGain();
    state.botGain.gain.value = 1;
    // Silent sink so tap nodes are pulled by the render graph with no audible output.
    state.tapSink = created.createGain();
    state.tapSink.gain.value = 0;
    state.tapSink.connect(created.destination);
    try {
      ours.add(state.tapSink);
      ours.add(state.botGain);
    } catch {
      /* noop */
    }
    return created;
  }

  function emitToContent(track: string, buf: ArrayBuffer) {
    noteTapRms(track, buf);
    try {
      w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'audio', track, pcm: buf }, '*', [buf]);
    } catch {
      try {
        w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'audio', track, pcm: buf }, '*');
      } catch {
        /* noop */
      }
    }
  }

  // --- per-tap level reporting: RMS diag every 2 s per tap (auto-pick evidence) ---
  const tapStats: Record<string, { sum: number; n: number }> = {};
  function noteTapRms(track: string, buf: ArrayBuffer) {
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
    } catch {
      /* noop */
    }
  }
  try {
    setInterval(() => {
      try {
        Object.keys(tapStats).forEach((label) => {
          const st = tapStats[label];
          const lvl = batchLevel(new Int16Array([]).buffer); // placeholder, replaced below
          void lvl;
          const ms = st.n ? st.sum / st.n : 0;
          let db = ms > 0 ? 10 * Math.log10(ms) : -999;
          if (!isFinite(db)) db = -999;
          w.postMessage(
            {
              source: 'phathom',
              direction: 'inject-to-content',
              type: 'diag',
              key: 'rms',
              data: { label, rms: +Math.sqrt(ms || 0).toFixed(4), db: +db.toFixed(1) },
            },
            '*',
          );
          st.sum = 0;
          st.n = 0;
        });
      } catch {
        /* noop */
      }
    }, 2000);
  } catch {
    /* noop */
  }

  const diagSeen = new Set<string>();
  function diag(key: string, data?: unknown) {
    try {
      if (diagSeen.has(key)) return;
      diagSeen.add(key);
      log('diag ' + key, data || '');
      w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'diag', key, data: data ?? null }, '*');
    } catch {
      /* noop */
    }
  }

  // Nodes we create ourselves; the AudioNode.connect hook must ignore them.
  const ours = new WeakSet<object>();
  const sinks = new WeakMap<AudioContext, GainNode>();
  const modules = new WeakMap<AudioContext, Promise<void>>();

  function sinkFor(ctx: AudioContext): GainNode {
    const live = state.ctx;
    if (live && ctx === live && state.tapSink) return state.tapSink;
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

  async function makeTapNode(track: string, ctxArg?: AudioContext): Promise<TapRef> {
    const ctx = ctxArg || ensureCtx();
    const url = state.workletUrl;
    if (url) {
      try {
        let p = modules.get(ctx);
        if (!p) {
          p = (ctx.audioWorklet as AudioWorklet).addModule(url);
          modules.set(ctx, p);
        }
        await p;
        const node = new AudioWorkletNode(ctx, 'pcm-tap', { processorOptions: { track } });
        ours.add(node);
        node.connect(sinkFor(ctx));
        (node.port as MessagePort).onmessage = (ev: MessageEvent) => {
          try {
            const d = (ev.data || {}) as { pcm?: ArrayBuffer; track?: string };
            if (d.pcm) emitToContent(d.track || track, d.pcm);
          } catch {
            /* noop */
          }
        };
        if (state.workletMode === 'pending') {
          state.workletMode = 'worklet';
          log('worklet loaded OK (track=' + track + ')');
          reportReady();
        }
        return { node, kind: 'worklet' };
      } catch (e) {
        warn('worklet addModule failed, using ScriptProcessor fallback:', String((e as Error)?.message || e));
      }
    }
    try {
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      ours.add(proc);
      const framer = makeFramer(track, emitToContent);
      let carry = 0;
      proc.onaudioprocess = (ev: AudioProcessingEvent) => {
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
        } catch {
          /* noop */
        }
      };
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
      w.postMessage(
        {
          source: 'phathom',
          direction: 'inject-to-content',
          type: 'ready',
          hooks: { gum: !!hooksOk.gum, pc: !!hooksOk.pc },
          worklet: state.workletMode,
          frame: { href: location.href, isTop: w === w.top },
        },
        '*',
      );
    } catch {
      /* noop */
    }
  }

  // --- a) Mic mixer: wrap getUserMedia (fresh graph per call) ---
  function installGum() {
    try {
      const md = navigator.mediaDevices;
      if (!md || !md.getUserMedia) {
        warn('no mediaDevices.getUserMedia to wrap');
        return;
      }
      const orig = md.getUserMedia.bind(md);
      md.getUserMedia = async function (constraints?: MediaStreamConstraints) {
        const stream = await orig(constraints as MediaStreamConstraints);
        try {
          const wantsAudio = !!(constraints && (constraints as MediaStreamConstraints).audio);
          if (!wantsAudio) return stream;
          const ctx = ensureCtx();
          if (ctx.state === 'suspended') {
            ctx.resume().catch(() => {});
          }
          const audioTracks = stream.getAudioTracks();
          if (!audioTracks || audioTracks.length === 0) return stream;
          // Fresh graph per stream: realMic -> micGain -> dest (+ shared botGain -> dest).
          const src = ctx.createMediaStreamSource(stream);
          const micGain = ctx.createGain();
          micGain.gain.value = 1;
          const dest = ctx.createMediaStreamDestination();
          src.connect(micGain);
          micGain.connect(dest);
          (state.botGain as GainNode).connect(dest);
          state.micGain = micGain;
          let tap: TapRef | null = null;
          try {
            tap = await makeTapNode('owner');
            micGain.connect(tap.node);
          } catch (e) {
            warn('owner tap failed, mic still passes through:', e);
          }
          let torn = false;
          const teardown = () => {
            if (torn) return;
            torn = true;
            try {
              src.disconnect();
            } catch {
              /* noop */
            }
            try {
              micGain.disconnect();
            } catch {
              /* noop */
            }
            try {
              (state.botGain as GainNode).disconnect(dest);
            } catch {
              /* noop */
            }
            try {
              if (tap) tap.node.disconnect();
            } catch {
              /* noop */
            }
            log('mixer stream torn down');
          };
          const videoTracks = stream.getVideoTracks ? stream.getVideoTracks() : [];
          const out = new MediaStream();
          dest.stream.getAudioTracks().forEach((t: MediaStreamTrack) => {
            out.addTrack(t);
            const origStop = t.stop.bind(t);
            (t as MediaStreamTrack).stop = function () {
              try {
                origStop();
              } catch {
                /* noop */
              }
              try {
                audioTracks.forEach((rt: MediaStreamTrack) => rt.stop());
              } catch {
                /* noop */
              }
              teardown();
            };
          });
          videoTracks.forEach((t: MediaStreamTrack) => out.addTrack(t));
          log('getUserMedia wrapped: returning mixed stream (mic->mixer, owner tap on)');
          return out;
        } catch (e) {
          warn('mixer setup failed, returning original stream:', e);
          return stream;
        }
      };
      try {
        const nav = navigator as unknown as Record<string, unknown>;
        if (nav['getUserMedia'] && !(nav['getUserMedia'] as Record<string, unknown>).__phathom) {
          const oget = ((navigator as unknown as Record<string, (...a: never[]) => unknown>)['getUserMedia'] as (...a: never[]) => unknown).bind(navigator);
          const wrapped = function (...args: never[]) {
            try {
              return (md.getUserMedia as (...a: never[]) => Promise<MediaStream>)(...args).then(
                (args as unknown[])[1] as () => void,
                (args as unknown[])[2] as () => void,
              );
            } catch {
              try {
                return oget(...args);
              } catch (e2) {
                const err = (args as unknown[])[2] as ((e: unknown) => void) | undefined;
                if (err) err(e2);
              }
            }
          };
          (wrapped as unknown as Record<string, unknown>).__phathom = true;
          nav['getUserMedia'] = wrapped;
        }
      } catch {
        /* noop */
      }
      hooksOk.gum = true;
      log('getUserMedia hook installed');
    } catch (e) {
      warn('installGum failed:', e);
    }
  }

  // --- b) Remote audio tap: wrap RTCPeerConnection (webrtc source) ---
  function installPc() {
    try {
      const Orig = w.RTCPeerConnection;
      if (!Orig || Orig.__phathom) {
        if (Orig && Orig.__phathom) hooksOk.pc = true;
        return;
      }
      // NOTE: subclass keeps prototype/statics/name; never `new Orig()` inside a
      // wrapper (that breaks `class X extends AudioWorkletNode`-style subclasses).
      class PhathomPC extends Orig {
        constructor(...args: never[]) {
          super(...args);
          try {
            hookPcInstance(this as unknown as RTCPeerConnection);
          } catch {
            /* noop */
          }
        }
      }
      try {
        Object.getOwnPropertyNames(Orig).forEach((k: string) => {
          if (k === 'prototype' || k === 'name' || k === 'length') return;
          try {
            (PhathomPC as unknown as Record<string, unknown>)[k] = Orig[k];
          } catch {
            /* noop */
          }
        });
      } catch {
        /* noop */
      }
      try {
        Object.defineProperty(PhathomPC, 'name', { value: 'RTCPeerConnection' });
      } catch {
        /* noop */
      }
      (PhathomPC as unknown as Record<string, unknown>).__phathom = true;
      (PhathomPC as unknown as Record<string, unknown>).__orig = Orig;
      w.RTCPeerConnection = PhathomPC;
      hooksOk.pc = true;
      log('RTCPeerConnection hook installed');
    } catch (e) {
      warn('installPc failed:', e);
    }
  }

  function hookPcInstance(pc: RTCPeerConnection) {
    diag('pc_created');
    const onTrack = (ev: Event) => {
      try {
        const e = ev as RTCTrackEvent;
        diag('pc_track_' + (e && e.track && e.track.kind));
        const streams = (e && e.streams) || [];
        const track = e && e.track;
        if (!track || track.kind !== 'audio') return;
        state.callerCount += 1;
        const idx = state.callerCount;
        const label = idx === 1 ? 'caller' : 'caller:' + idx;
        if (idx > 1) warn('extra remote audio track (group call?); recording only caller:1, got ' + label);
        const ms = streams && streams[0] ? streams[0] : new MediaStream([track]);
        void tapRemoteStream(ms, label);
      } catch {
        /* noop */
      }
    };
    // addEventListener only — never override pc.ontrack (reviewer fix).
    try {
      pc.addEventListener('track', onTrack as EventListener);
    } catch {
      /* noop */
    }
    try {
      pc.addEventListener('connectionstatechange', () => {
        diag('pc_state_' + pc.connectionState);
        if (pc.connectionState === 'closed' || pc.connectionState === 'failed') state.callerCount = 0;
        if (pc.connectionState === 'connected') {
          try {
            const kinds = pc.getReceivers().map((r) => r.track && r.track.kind);
            diag('pc_receivers', kinds);
            if (state.callerCount === 0) {
              pc.getReceivers().forEach((r) => {
                if (r.track && r.track.kind === 'audio') {
                  state.callerCount += 1;
                  void tapRemoteStream(new MediaStream([r.track]), 'caller');
                }
              });
            }
          } catch {
            /* noop */
          }
        }
      });
    } catch {
      /* noop */
    }
  }

  // --- speaker taps (§5.7 source 2): every non-ours node → any destination, one tap per NODE ---
  const tappedNodes = new WeakSet<object>();
  function installSpeakerTap() {
    try {
      const proto = w.AudioNode && w.AudioNode.prototype;
      if (!proto || (proto.connect as unknown as Record<string, unknown>).__phathom) return;
      const origConnect = proto.connect;
      const wrapped = function (this: AudioNode, dest: AudioNode, ...rest: unknown[]) {
        const result = origConnect.call(this, dest, ...(rest as []));
        try {
          if (dest instanceof w.AudioDestinationNode && !ours.has(this) && !tappedNodes.has(this)) {
            tappedNodes.add(this);
            const ctx = (this as AudioNode).context;
            const cid = ctxId(ctx);
            const name = (this.constructor && this.constructor.name) || '?';
            state.speakerN += 1;
            const label = 'speaker:' + state.speakerN + '(' + name + ')';
            diag('dest_connect_' + name + '_' + ctx.sampleRate + '_' + cid + '_' + state.speakerN, {
              node: name,
              rate: ctx.sampleRate,
              ctx: cid,
              label,
            });
            const node: AudioNode = this;
            makeTapNode(label, ctx as AudioContext)
              .then((tap) => {
                try {
                  origConnect.call(node, tap.node);
                  diag('tap_on_' + label + '_' + tap.kind);
                } catch {
                  /* noop */
                }
              })
              .catch((e) => diag('tap_failed_' + label, String((e as Error)?.message || e)));
          }
        } catch {
          /* noop */
        }
        return result;
      };
      (wrapped as unknown as Record<string, unknown>).__phathom = true;
      proto.connect = wrapped;
      log('AudioNode.connect hook installed');
    } catch (e) {
      warn('installSpeakerTap failed:', e);
    }
  }

  // wrapCtor: Reflect.construct + new.target so subclassing keeps working.
  function wrapCtor<T extends new (...args: never[]) => unknown>(Orig: T, name: string, onCreate: (obj: unknown, args: unknown[]) => void): T {
    const W = function (this: unknown, ...args: never[]) {
      if (!(this instanceof (W as unknown as new (...a: never[]) => unknown)))
        return (Orig as unknown as (...a: never[]) => unknown).apply(this, args);
      const obj = Reflect.construct(Orig as unknown as new (...a: never[]) => object, args, new.target === W ? (Orig as unknown as new (...a: never[]) => object) : (new.target as unknown as new (...a: never[]) => object));
      try {
        onCreate(obj, args as unknown[]);
      } catch {
        /* noop */
      }
      return obj;
    };
    W.prototype = (Orig as unknown as { prototype: unknown }).prototype as object;
    try {
      Object.setPrototypeOf(W, Orig);
    } catch {
      /* noop */
    }
    try {
      Object.defineProperty(W, 'name', { value: name });
    } catch {
      /* noop */
    }
    (W as unknown as Record<string, unknown>).__phathom = true;
    return W as unknown as T;
  }

  const ctxIds = new WeakMap<object, number>();
  let ctxSeq = 0;
  function ctxId(ctx: object | null): number {
    try {
      if (!ctx || !ctxIds.has(ctx)) {
        ctxSeq += 1;
        if (ctx) ctxIds.set(ctx, ctxSeq);
        return ctxSeq;
      }
      return ctxIds.get(ctx) as number;
    } catch {
      return 0;
    }
  }

  function installCtxHook() {
    try {
      const Orig = w.AudioContext;
      if (!Orig || Orig.__phathom) return;
      w.AudioContext = wrapCtor(Orig, 'AudioContext', (c, args) => {
        let hint = '';
        try {
          hint = ((args[0] as Record<string, unknown>)?.['latencyHint'] as string) || '';
        } catch {
          /* noop */
        }
        diag('ctx_' + ctxId(c as object) + '_' + (c as AudioContext).sampleRate, {
          rate: (c as AudioContext).sampleRate,
          hint: String(hint),
        });
      });
      log('AudioContext hook installed');
    } catch (e) {
      warn('installCtxHook failed:', e);
    }
    try {
      const P = w.AudioContext && w.AudioContext.prototype;
      if (P && P.setSinkId && !(P.setSinkId as unknown as Record<string, unknown>).__phathom) {
        const orig = P.setSinkId;
        const f = function (this: AudioContext, deviceId: string, ...rest: unknown[]) {
          try {
            diag('setsinkid_ctx' + ctxId(this as object) + '_' + String(deviceId).slice(0, 60));
          } catch {
            /* noop */
          }
          return orig.call(this, deviceId, ...(rest as []));
        };
        (f as unknown as Record<string, unknown>).__phathom = true;
        P.setSinkId = f;
      }
    } catch {
      /* noop */
    }
  }

  function installWorkletNodeHook() {
    try {
      const Orig = w.AudioWorkletNode;
      if (!Orig || Orig.__phathom) return;
      w.AudioWorkletNode = wrapCtor(Orig, 'AudioWorkletNode', (node, args) => {
        const n = node as AudioWorkletNode;
        diag(
          'workletnode_' + String((args[1] as string) ?? '').slice(0, 60) + '_ctx' + ctxId(args[0] as object) + '_io' + n.numberOfInputs + 'x' + n.numberOfOutputs,
        );
      });
      log('AudioWorkletNode hook installed');
    } catch (e) {
      warn('installWorkletNodeHook failed:', e);
    }
  }

  function installMediaPlayHook() {
    try {
      const P = w.HTMLMediaElement && w.HTMLMediaElement.prototype;
      if (!P || (P.play as unknown as Record<string, unknown>).__phathom) return;
      const orig = P.play;
      const f = function (this: HTMLMediaElement, ...args: unknown[]) {
        try {
          let kind = 'none';
          try {
            const s = (this as HTMLMediaElement).srcObject;
            kind = s
              ? 'stream:' + (((s as MediaStream).getTracks && (s as MediaStream).getTracks().map((t) => t.kind).join(',')) || '?')
              : 'attr:' + String((this as HTMLMediaElement).currentSrc || (this as HTMLMediaElement).src || 'empty').slice(-80);
          } catch {
            /* noop */
          }
          let sink = '';
          try {
            sink = (this as unknown as Record<string, string>).sinkId || '';
          } catch {
            /* noop */
          }
          diag('media_play_' + kind + '_sink_' + String(sink).slice(0, 40));
        } catch {
          /* noop */
        }
        return orig.apply(this, args as []);
      };
      (f as unknown as Record<string, unknown>).__phathom = true;
      P.play = f;
      log('HTMLMediaElement.play hook installed');
    } catch (e) {
      warn('installMediaPlayHook failed:', e);
    }
    try {
      const P = w.AudioContext && w.AudioContext.prototype;
      if (P && P.createMediaStreamDestination && !(P.createMediaStreamDestination as unknown as Record<string, unknown>).__phathom) {
        const orig = P.createMediaStreamDestination;
        const f = function (this: AudioContext, ...args: unknown[]) {
          try {
            diag('createmediadest_ctx' + ctxId(this as object));
          } catch {
            /* noop */
          }
          return orig.apply(this, args as []);
        };
        (f as unknown as Record<string, unknown>).__phathom = true;
        P.createMediaStreamDestination = f;
      }
    } catch {
      /* noop */
    }
  }

  function installProbes() {
    try {
      const AW = w.AudioWorklet && w.AudioWorklet.prototype;
      if (AW && !(AW.addModule as unknown as Record<string, unknown>).__phathom) {
        const orig = AW.addModule;
        AW.addModule = function (url: string, ...rest: unknown[]) {
          try {
            if (String(url) !== state.workletUrl) diag('worklet_module_' + String(url).slice(0, 120));
          } catch {
            /* noop */
          }
          return orig.call(this, url, ...(rest as []));
        };
        (AW.addModule as unknown as Record<string, unknown>).__phathom = true;
      }
    } catch {
      /* noop */
    }
    try {
      const OrigWorker = w.Worker;
      if (OrigWorker && !OrigWorker.__phathom) {
        w.Worker = wrapCtor(OrigWorker, 'Worker', (_wv, args) => {
          diag('worker_' + String(args[0]).slice(0, 120));
        });
      }
    } catch {
      /* noop */
    }
    try {
      const desc = Object.getOwnPropertyDescriptor(w.HTMLMediaElement.prototype, 'srcObject');
      if (desc && desc.set && !(desc.set as unknown as Record<string, unknown>).__phathom) {
        const set = function (this: HTMLMediaElement, v: unknown) {
          try {
            const kinds = v && (v as MediaStream).getTracks ? (v as MediaStream).getTracks().map((t) => t.kind).join(',') : typeof v;
            diag('media_srcObject_' + kinds);
          } catch {
            /* noop */
          }
          return desc.set!.call(this, v);
        };
        (set as unknown as Record<string, unknown>).__phathom = true;
        Object.defineProperty(w.HTMLMediaElement.prototype, 'srcObject', { ...desc, set });
      }
    } catch {
      /* noop */
    }
  }

  async function tapRemoteStream(mediaStream: MediaStream, label: string) {
    try {
      const ctx = ensureCtx();
      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
      const src = ctx.createMediaStreamSource(mediaStream);
      const tap = await makeTapNode(label);
      src.connect(tap.node);
      // Chromium can feed silence to WebAudio unless the stream is also on a media element.
      try {
        const a = new Audio();
        a.muted = true;
        a.srcObject = mediaStream;
        const p = a.play();
        if (p && (p as Promise<void>).catch) (p as Promise<void>).catch(() => {});
      } catch {
        /* noop */
      }
      log('remote tap on (' + label + ') via ' + tap.kind);
    } catch (e) {
      warn('tapRemoteStream failed (' + label + '), call continues untapped:', e);
    }
  }

  // --- d) Bot playback (§5.3d): gap-free 24 kHz PCM + test tone + stop_audio ---
  function playTestTone(freq?: number, durationS?: number) {
    freq = freq || 440;
    durationS = durationS || 3;
    try {
      const ctx = ensureCtx();
      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
      const osc = ctx.createOscillator();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const g = ctx.createGain();
      const t = ctx.currentTime;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.5, t + 0.05);
      g.gain.setValueAtTime(0.5, t + durationS - 0.05);
      g.gain.exponentialRampToValueAtTime(0.0001, t + durationS);
      osc.connect(g);
      g.connect(state.botGain as GainNode);
      state.botSources.add(osc);
      const done = () => {
        try {
          state.botSources.delete(osc);
        } catch {
          /* noop */
        }
        try {
          g.disconnect();
        } catch {
          /* noop */
        }
        try {
          w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'tone_ended', freq, durationS }, '*');
        } catch {
          /* noop */
        }
      };
      osc.onended = done;
      osc.start(t);
      osc.stop(t + durationS + 0.05);
      log('playing test tone ' + freq + 'Hz ' + durationS + 's through bot path');
      w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'tone_started', freq, durationS }, '*');
      return true;
    } catch (e) {
      warn('playTestTone failed:', e);
      return false;
    }
  }

  /** Schedule one 24 kHz s16le mono chunk gap-free on botGain (server streams play_begin/chunks). */
  let playCursor = 0;
  function playBotChunk(id: number, pcm: ArrayBuffer) {
    try {
      const ctx = ensureCtx();
      if (ctx.state === 'suspended') {
        ctx.resume().catch(() => {});
      }
      const n = pcm.byteLength / 2;
      if (n === 0) return;
      const buf = ctx.createBuffer(1, n, 24000);
      const ch = buf.getChannelData(0);
      const v = new Int16Array(pcm);
      for (let i = 0; i < n; i++) ch[i] = v[i] / 0x8000;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(state.botGain as GainNode);
      const now = ctx.currentTime;
      if (playCursor < now) playCursor = now;
      src.start(playCursor);
      playCursor += n / 24000;
      let arr = state.playSeq.get(id);
      if (!arr) {
        arr = [];
        state.playSeq.set(id, arr);
      }
      arr.push(src);
      state.botSources.add(src);
      src.onended = () => {
        try {
          state.botSources.delete(src);
        } catch {
          /* noop */
        }
      };
    } catch (e) {
      warn('playBotChunk failed:', e);
    }
  }

  function playEnd(id: number, completed = true) {
    try {
      const arr = state.playSeq.get(id) || [];
      state.playSeq.delete(id);
      if (arr.length === 0) {
        finishPlayed(id, completed);
        return;
      }
      const last = arr[arr.length - 1];
      // Ack when the last buffer ends (or immediately if already silent).
      const t = window.setTimeout(() => finishPlayed(id, completed), 50);
      void t;
      try {
        last.onended = () => {
          try {
            state.botSources.delete(last);
          } catch {
            /* noop */
          }
          finishPlayed(id, completed);
        };
      } catch {
        finishPlayed(id, completed);
      }
    } catch {
      /* noop */
    }
  }

  const playedSent = new Set<number>();
  function finishPlayed(id: number, completed: boolean) {
    try {
      if (playedSent.has(id)) return;
      playedSent.add(id);
      if (playedSent.size > 50) {
        const first = playedSent.values().next().value as number;
        playedSent.delete(first);
      }
      w.postMessage({ source: 'phathom', direction: 'inject-to-content', type: 'played', id, completed }, '*');
    } catch {
      /* noop */
    }
  }

  function stopAllBotAudio() {
    try {
      playCursor = 0;
      state.playSeq.forEach((arr, id) => {
        arr.forEach((s) => {
          try {
            s.stop();
          } catch {
            /* noop */
          }
        });
        finishPlayed(id, false);
      });
      state.playSeq.clear();
      state.botSources.forEach((s) => {
        try {
          (s as AudioScheduledSourceNode).stop();
        } catch {
          /* noop */
        }
      });
      state.botSources.clear();
    } catch {
      /* noop */
    }
  }

  // --- e) Control surface (content.js ⇄ inject.js), nonce-gated ---
  w.addEventListener('message', (ev: MessageEvent) => {
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
        } catch {
          /* noop */
        }
        return;
      }
      if (d.nonce !== state.nonce) return; // ignore spoofed commands
      if (d.type === 'play_tone') {
        playTestTone(d.freq || 440, d.durationS || 3);
      } else if (d.type === 'play_begin') {
        playCursor = 0;
        playedSent.delete(d.id);
      } else if (d.type === 'play_chunk') {
        if (d.pcm instanceof ArrayBuffer) playBotChunk(d.id, d.pcm);
      } else if (d.type === 'play_end') {
        playEnd(d.id, true);
      } else if (d.type === 'stop_audio') {
        stopAllBotAudio();
      } else if (d.type === 'set_gain') {
        try {
          if (state.ctx) {
            if (typeof d.mic === 'number' && state.micGain) state.micGain.gain.value = d.mic;
            if (typeof d.bot === 'number' && (state.botGain as GainNode)) (state.botGain as GainNode).gain.value = d.bot;
            log('gains set mic=' + (state.micGain ? state.micGain.gain.value : 'n/a') + ' bot=' + (state.botGain as GainNode).gain.value);
          }
        } catch {
          /* noop */
        }
      }
    } catch {
      /* noop */
    }
  });

  installCtxHook();
  installWorkletNodeHook();
  installGum();
  installPc();
  installSpeakerTap();
  installMediaPlayHook();
  installProbes();
  try {
    document.addEventListener('DOMContentLoaded', () => {
      try {
        reportReady();
      } catch {
        /* noop */
      }
    });
  } catch {
    /* noop */
  }
})();
