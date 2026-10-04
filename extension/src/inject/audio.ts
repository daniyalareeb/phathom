// Pure audio helpers shared by inject.ts and vitest.
// 16 kHz s16le framing: 30 ms frames (960 samples) batched as 100 ms chunks
// (3200 bytes) — matches the server FRAME_BYTES=960 contract.

export const OUT_RATE = 16000;
export const FRAME_SAMPLES = 480; // 30 ms @16 kHz
export const FRAMES_PER_BATCH = 3; // 90–100 ms per postMessage

export type BatchHandler = (track: string, buf: ArrayBuffer) => void;

/** Collect float samples, emit s16le ArrayBuffers of FRAME*batch samples. */
export function makeFramer(track: string, onBatch: BatchHandler) {
  let pending: number[] = [];
  const target = FRAME_SAMPLES * FRAMES_PER_BATCH;
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

export type Framer = ReturnType<typeof makeFramer>;

/** Mean-square → dBFS, for per-tap level meters. Silence is -Infinity. */
export function meanSquareDb(ms: number): number {
  return ms > 0 ? 10 * Math.log10(ms) : -Infinity;
}

/** RMS of one s16le batch, as {rms, db} for the Diagnostics level meters. */
export function batchLevel(buf: ArrayBuffer): { rms: number; db: number } {
  const v = new Int16Array(buf);
  if (v.length === 0) return { rms: 0, db: -999 };
  let s = 0;
  for (let i = 0; i < v.length; i++) {
    const f = v[i] / 0x8000;
    s += f * f;
  }
  const ms = s / v.length;
  let db = meanSquareDb(ms);
  if (!isFinite(db)) db = -999;
  return { rms: +Math.sqrt(ms).toFixed(4), db: +db.toFixed(1) };
}

/** Linear resample of one mono float block to 16 kHz float samples. */
export function resampleBlock(input: Float32Array, inRate: number, carry: { pos: number }): number[] {
  const out: number[] = [];
  const ratio = inRate / OUT_RATE;
  let pos = carry.pos;
  while (pos < input.length) {
    const i0 = Math.floor(pos);
    const i1 = Math.min(input.length - 1, i0 + 1);
    const frac = pos - i0;
    out.push(input[i0] * (1 - frac) + input[i1] * frac);
    pos += ratio;
  }
  carry.pos = pos - input.length;
  return out;
}
