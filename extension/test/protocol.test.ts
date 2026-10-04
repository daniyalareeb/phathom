import { describe, expect, it } from 'vitest';
import { encodeAudioFrame, decodeAudioHeader, bytesToB64, b64ToBytes, KIND_CALLER, KIND_TAB, KIND_BOT } from '../src/shared/protocol';

describe('binary audio frames (§6.1)', () => {
  it('round-trips kind + seq + pcm', () => {
    const pcm = new Uint8Array([1, 2, 3, 4]);
    const frame = encodeAudioFrame(KIND_CALLER, 42, pcm);
    expect(frame[0]).toBe(KIND_CALLER);
    expect(frame.length).toBe(9);
    const hdr = decodeAudioHeader(frame)!;
    expect(hdr.kind).toBe(KIND_CALLER);
    expect(hdr.seq).toBe(42);
    expect(Array.from(frame.slice(5))).toEqual([1, 2, 3, 4]);
  });

  it('supports the tab-capture kind 0x04 and bot kind 0x11', () => {
    expect(decodeAudioHeader(encodeAudioFrame(KIND_TAB, 7, new Uint8Array([9])))!.kind).toBe(KIND_TAB);
    expect(decodeAudioHeader(encodeAudioFrame(KIND_BOT, 1, new Uint8Array([9])))!.kind).toBe(KIND_BOT);
  });

  it('rejects short frames', () => {
    expect(decodeAudioHeader(new Uint8Array([1, 2]))).toBeNull();
  });
});

describe('base64 port relay', () => {
  it('round-trips binary pcm', () => {
    const pcm = new Uint8Array([0, 255, 16, 32, 200]);
    expect(b64ToBytes(bytesToB64(pcm))).toEqual(pcm);
  });
});
