// offscreen.ts — offscreen document for tabCapture (§5.7 source 3).
// Plays the captured tab stream back to the speakers (capture mutes the tab
// otherwise), runs the same 16 kHz tap, and forwards frames as track `tab`
// only between call_started and call_ended.
// NOTE: no imports — this entry must bundle to ONE self-contained file
// (MV3 content/offscreen scripts cannot load shared chunks).

(function () {
  'use strict';

  // Local copy of makeFramer (see src/inject/audio.ts): 30 ms frames @16 kHz,
  // batched as 3-frame (~90 ms) s16le ArrayBuffers.
  function makeFramer(track: string, onBatch: (track: string, buf: ArrayBuffer) => void) {
    let pending: number[] = [];
    const target = 480 * 3;
    return {
      push(v: number) {
        let s = Math.max(-1, Math.min(1, v));
        s = s < 0 ? s * 0x8000 : s * 0x7fff;
        pending.push(s | 0);
        if (pending.length >= target) {
          const buf = new ArrayBuffer(target * 2);
          const view = new DataView(buf);
          for (let i = 0; i < target; i++) view.setInt16(i * 2, pending[i], true);
          pending = pending.slice(target);
          try {
            onBatch(track, buf);
          } catch {
            /* never throw into the audio thread */
          }
        }
      },
    };
  }
  let inCall = false;
  let port: chrome.runtime.Port | null = null;
  let ctx: AudioContext | null = null;

  function connectPort() {
    try {
      port = chrome.runtime.connect({ name: 'audio' });
      port.onDisconnect.addListener(() => {
        port = null;
        setTimeout(connectPort, 1000);
      });
    } catch {
      setTimeout(connectPort, 1000);
    }
  }
  connectPort();

  function sendToServer(msg: Record<string, unknown>) {
    try {
      if (port) port.postMessage(msg);
    } catch {
      /* noop */
    }
  }

  function b64(buf: ArrayBuffer): string {
    const bytes = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x2000) {
      s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x2000)));
    }
    return btoa(s);
  }

  chrome.runtime.onMessage.addListener((msg: Record<string, unknown>, _s, sendResponse) => {
    (async () => {
      try {
        if (msg.cmd === 'offscreen_capture') {
          await startCapture(String(msg.streamId));
          sendResponse({ ok: true });
        } else if (msg.t === 'live' && (msg.event as { type: string })?.type === 'call_started') {
          inCall = true;
          sendResponse({ ok: true });
        } else if (msg.t === 'live' && (msg.event as { type: string })?.type === 'call_ended') {
          inCall = false;
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

  async function startCapture(streamId: string) {
    try {
      const stream = await (navigator.mediaDevices as MediaDevices).getUserMedia({
        audio: {
          mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId },
        } as unknown as MediaTrackConstraints,
        video: false,
      });
      ctx = new AudioContext({ sampleRate: 48000 });
      const src = ctx.createMediaStreamSource(stream);
      // Play back to the speakers: tabCapture mutes the tab otherwise.
      const speakers = ctx.createGain();
      speakers.gain.value = 1;
      src.connect(speakers);
      speakers.connect(ctx.destination);
      // Same pcm-tap at 16 kHz.
      const framer = makeFramer('tab', (track, buf) => {
        if (!inCall) return;
        sendToServer({ t: 'audio', track, pcmBase64: b64(buf) });
      });
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const sink = ctx.createGain();
      sink.gain.value = 0;
      sink.connect(ctx.destination);
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
      src.connect(proc);
      proc.connect(sink);
      sendToServer({ t: 'diag', key: 'tab_capture_on', data: null });
    } catch (e) {
      sendToServer({ t: 'diag', key: 'tab_capture_failed', data: String(e).slice(0, 200) });
    }
  }
})();
