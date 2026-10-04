import { describe, expect, it } from 'vitest';
import spike from '../inject.js';

// inject.js exports its pure (DOM-free) helpers when loaded as a module;
// the browser IIFE body is skipped in node (no window).
const { makeFramer, meanSquareDb } = spike;

describe('makeFramer (16 kHz s16le batching)', () => {
  it('emits 2880-byte batches after 1440 samples', () => {
    const batches = [];
    const framer = makeFramer('caller', (track, buf) => batches.push({ track, buf }));
    for (let i = 0; i < 1439; i++) framer.push(0.5);
    expect(batches).toHaveLength(0);
    framer.push(0.5);
    expect(batches).toHaveLength(1);
    expect(batches[0].track).toBe('caller');
    expect(batches[0].buf.byteLength).toBe(2880);
  });

  it('clips and encodes full-scale correctly', () => {
    const batches = [];
    const framer = makeFramer('owner', (track, buf) => batches.push(buf));
    for (let i = 0; i < 1440; i++) framer.push(2.0); // clips to 1.0
    const view = new DataView(batches[0]);
    for (let i = 0; i < 1440; i++) expect(view.getInt16(i * 2, true)).toBe(0x7fff);
  });

  it('encodes silence as zeros', () => {
    const batches = [];
    const framer = makeFramer('caller', (track, buf) => batches.push(buf));
    for (let i = 0; i < 1440; i++) framer.push(0);
    const view = new DataView(batches[0]);
    for (let i = 0; i < 1440; i++) expect(view.getInt16(i * 2, true)).toBe(0);
  });
});

describe('meanSquareDb (tap level reporting)', () => {
  it('full-scale sine is near -3 dBFS', () => {
    // mean square of a full-scale sine wave is 0.5 -> -3.01 dB
    expect(meanSquareDb(0.5)).toBeCloseTo(-3.01, 1);
  });

  it('silence is -Infinity', () => {
    expect(meanSquareDb(0)).toBe(-Infinity);
  });

  it('distinguishes speech-level from noise-floor', () => {
    const speechDb = meanSquareDb(0.01); // -20 dBFS
    const noiseDb = meanSquareDb(1e-9); // -90 dBFS
    expect(speechDb).toBeGreaterThan(noiseDb + 40);
  });
});
