// Shared protocol types: extension ⇄ server (EXTENSION_SPEC §6.1).
// Text frames are JSON {t, ...}; binary frames are [kind:1][seq u32LE][PCM].

export const KIND_CALLER = 0x01; // ext → server, caller 16 kHz
export const KIND_OWNER = 0x02; // ext → server, owner (mic) 16 kHz
export const KIND_SPEAKER = 0x03; // ext → server, legacy speaker tap 16 kHz
export const KIND_TAB = 0x04; // ext → server, tab-capture 16 kHz (§5.7)
export const KIND_BOT = 0x11; // server → ext, bot speech 24 kHz

export type ExtToServer =
  | { t: 'hello'; ext_version: string; tab_id?: number }
  | { t: 'wa_status'; logged_in: boolean }
  | { t: 'ring'; caller: string; is_video: boolean }
  | { t: 'ring_gone' }
  | { t: 'call_started'; direction: 'incoming' | 'outgoing'; peer: string; answered_by: 'owner' | 'bot' }
  | { t: 'call_ended'; reason: string }
  | { t: 'played'; id: number; completed: boolean }
  | { t: 'ui_action'; action: 'hangup' | 'take_over' | 'hand_back' | 'note'; text?: string }
  | { t: 'pong' };

export type ServerToExt =
  | { t: 'welcome'; state: UiState }
  | { t: 'state'; state: UiState }
  | { t: 'accept' }
  | { t: 'hangup' }
  | { t: 'play_begin'; id: number }
  | { t: 'play_end'; id: number }
  | { t: 'stop_audio' }
  | { t: 'set_gain'; mic: number; bot: number }
  | { t: 'live'; call_id: string | null; event: LiveEvent }
  | { t: 'ping' };

export interface UiState {
  connected: boolean;
  mode: string;
  answer?: string;
  copilot: boolean;
  answer_delay_s: number;
  live_transcript: boolean;
  side_panel_auto: boolean;
  wa_logged_in?: boolean | null;
  current_call?: { id: string; caller: string; since: string; kind: string } | null;
  in_call?: boolean;
}

export type LiveEvent =
  | { type: 'ring'; caller: string; is_video: boolean }
  | { type: 'call_started'; kind: string; peer: string; direction: string }
  | { type: 'call_ended'; reason: string }
  | { type: 'transcript'; speaker: string; text: string; t: number }
  | { type: 'transcript_paused'; reason: string }
  | { type: 'summary_ready' }
  | { type: 'note_saved'; text: string }
  | { type: 'idle'; reason: string };

/** Encode one ext→server binary audio frame. Pure: unit-tested. */
export function encodeAudioFrame(kind: number, seq: number, pcm: Uint8Array): Uint8Array {
  const frame = new Uint8Array(1 + 4 + pcm.length);
  frame[0] = kind & 0xff;
  frame[1] = seq & 0xff;
  frame[2] = (seq >> 8) & 0xff;
  frame[3] = (seq >> 16) & 0xff;
  frame[4] = (seq >> 24) & 0xff;
  frame.set(pcm, 5);
  return frame;
}

/** Decode a binary frame header. Returns null when too short. */
export function decodeAudioHeader(frame: Uint8Array): { kind: number; seq: number } | null {
  if (frame.length < 5) return null;
  const seq =
    (frame[1] | (frame[2] << 8) | (frame[3] << 16) | (frame[4] << 24)) >>> 0;
  return { kind: frame[0], seq };
}

/** Base64 helpers for the content ⇄ background Port (runtime messaging is JSON). */
export function bytesToB64(bytes: Uint8Array): string {
  let s = '';
  const CH = 8192;
  for (let i = 0; i < bytes.length; i += CH) {
    s += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CH)));
  }
  // btoa exists in all extension contexts.
  return btoa(s);
}

export function b64ToBytes(b64: string): Uint8Array | null {
  try {
    const s = atob(b64);
    const u = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    return u;
  } catch {
    return null;
  }
}
