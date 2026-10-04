import { describe, expect, it } from 'vitest';
import { makeFramer, meanSquareDb, batchLevel, resampleBlock, FRAME_SAMPLES, FRAMES_PER_BATCH } from '../src/inject/audio';

describe('makeFramer (16 kHz s16le batching)', () => {
  it('emits batches of 3x30ms frames after enough samples', () => {
    const batches: { track: string; buf: ArrayBuffer }[] = [];
    const framer = makeFramer('caller', (track, buf) => batches.push({ track, buf }));
    const target = FRAME_SAMPLES * FRAMES_PER_BATCH;
    for (let i = 0; i < target - 1; i++) framer.push(0.5);
    expect(batches).toHaveLength(0);
    framer.push(0.5);
    expect(batches).toHaveLength(1);
    expect(batches[0].track).toBe('caller');
    expect(batches[0].buf.byteLength).toBe(target * 2);
  });

  it('clips and encodes full-scale correctly', () => {
    const batches: ArrayBuffer[] = [];
    const framer = makeFramer('owner', (_t, buf) => batches.push(buf));
    for (let i = 0; i < FRAME_SAMPLES * FRAMES_PER_BATCH; i++) framer.push(2.0);
    const view = new DataView(batches[0]);
    for (let i = 0; i < FRAME_SAMPLES * FRAMES_PER_BATCH; i++) expect(view.getInt16(i * 2, true)).toBe(0x7fff);
  });

  it('encodes silence as zeros', () => {
    const batches: ArrayBuffer[] = [];
    const framer = makeFramer('caller', (_t, buf) => batches.push(buf));
    for (let i = 0; i < FRAME_SAMPLES * FRAMES_PER_BATCH; i++) framer.push(0);
    const view = new DataView(batches[0]);
    for (let i = 0; i < FRAME_SAMPLES * FRAMES_PER_BATCH; i++) expect(view.getInt16(i * 2, true)).toBe(0);
  });
});

describe('meanSquareDb (tap level reporting)', () => {
  it('full-scale sine is near -3 dBFS', () => {
    expect(meanSquareDb(0.5)).toBeCloseTo(-3.01, 1);
  });
  it('silence is -Infinity', () => {
    expect(meanSquareDb(0)).toBe(-Infinity);
  });
  it('distinguishes speech-level from noise-floor', () => {
    expect(meanSquareDb(0.01)).toBeGreaterThan(meanSquareDb(1e-9) + 40);
  });
});

describe('batchLevel (Diagnostics meters)', () => {
  it('silent batch reports the noise floor', () => {
    const lvl = batchLevel(new ArrayBuffer(960));
    expect(lvl.db).toBe(-999);
  });
});

describe('resampleBlock (48k -> 16k)', () => {
  it('downsamples 3:1', () => {
    const inp = new Float32Array(480);
    inp.fill(0.5);
    const out = resampleBlock(inp, 48000, { pos: 0 });
    expect(out.length).toBe(160);
    expect(out[0]).toBeCloseTo(0.5, 5);
  });
});
