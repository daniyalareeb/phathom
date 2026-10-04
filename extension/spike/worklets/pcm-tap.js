// pcm-tap AudioWorklet for MyPhathom Phase 0 spike.
// Input: float32 @ context sampleRate (typically 48 kHz). Output via port:
//   { track: string, pcm: ArrayBuffer } where pcm is s16le mono 16 kHz,
//   posted in ~90 ms batches (4320 samples-in equivalent; 1440 out samples = 2880 bytes).
// Keeps a fractional resample cursor so any input rate works.

class PcmTapProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.track = o.track || 'caller';
    this.outRate = 16000;
    this.frameSamples = 480; // 30 ms @16k
    this.batchFrames = 3; // 3x30ms = 90 ms per postMessage
    this._carry = 0; // fractional position in input samples
    this._pending = []; // int16 values not yet emitted
  }

  _pushSample(v) {
    let s = Math.max(-1, Math.min(1, v));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    this._pending.push(s | 0);
    if (this._pending.length >= this.frameSamples * this.batchFrames) {
      const n = this.frameSamples * this.batchFrames;
      const buf = new ArrayBuffer(n * 2);
      const view = new DataView(buf);
      for (let i = 0; i < n; i++) view.setInt16(i * 2, this._pending[i], true);
      this._pending.splice(0, n);
      this.port.postMessage({ track: this.track, pcm: buf }, [buf]);
    }
  }

  process(inputs) {
    try {
      const chs = inputs && inputs[0];
      if (!chs || chs.length === 0 || chs[0].length === 0) return true;
      // Downmix to mono.
      const n = chs[0].length;
      const mono = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        let s = 0;
        for (let c = 0; c < chs.length; c++) s += chs[c][i];
        mono[i] = s / chs.length;
      }
      // Linear resample: input sampleRate -> 16 kHz.
      const ratio = sampleRate / this.outRate;
      let pos = this._carry;
      while (pos < n) {
        const i0 = Math.floor(pos);
        const i1 = Math.min(n - 1, i0 + 1);
        const frac = pos - i0;
        const v = mono[i0] * (1 - frac) + mono[i1] * frac;
        this._pushSample(v);
        pos += ratio;
      }
      this._carry = pos - n;
    } catch (e) {
      // Never throw into the audio thread host; just skip the block.
    }
    return true;
  }
}

registerProcessor('pcm-tap', PcmTapProcessor);
