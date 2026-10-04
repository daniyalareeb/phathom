// Selectors ported 1:1 from phathom/whatsapp/selectors.py
// (discovered 2026-10-02, WhatsApp Web Chrome 151; call UI is in the SAME page).
// Rule: never invent selectors. Anything not found in data/dumps/*.html is
// marked UNVERIFIED and fails visibly (Diagnostics row + log line).

export const SEL = {
  LOGGED_IN: '[data-testid="chat-list"]',
  INCOMING_ROOT:
    '[data-testid="voip-container-audio-call"], [data-testid="voip-container-incoming-video-call"]',
  INCOMING_CALLER: '[data-testid="voip-call-participant-info-name"]',
  INCOMING_IS_VIDEO: '[data-testid="voip-container-incoming-video-call"]',
  CALL_TIMER: '[data-testid="voip-call-timer"]',
} as const;

export const ACCEPT_RE = /^Accept$/i;
export const DECLINE_RE = /^Decline$/i;
export const HANGUP_RE = /^End call$/i;
export const CAMERA_RE = /^Turn camera on$/i;

// UNVERIFIED: needs DOM dump — the outgoing call screen shows "Unknown" today
// (EXTENSION_SPEC current-status table). Candidate selectors to confirm from a
// real outgoing-call dump: the active-call peer name element.
// Until verified, outgoing peer names report "Unknown" + a Diagnostics row.
export const OUTGOING_PEER_CANDIDATES = [
  '[data-testid="voip-call-participant-info-name"]', // same element, outgoing screen // UNVERIFIED: needs DOM dump
  '[data-testid="voip-call-title"]', // UNVERIFIED: needs DOM dump
  '[data-testid="voip-voice-call-participant-name"]', // UNVERIFIED: needs DOM dump
] as const;

// UNVERIFIED: needs DOM dump — chat bubbles for the Brain "Read this chat"
// feature (§8.1). Only used after a dump confirms them.
export const CHAT_PANE = '[data-testid="conversation-panel-messages"]'; // UNVERIFIED: needs DOM dump
export const CHAT_BUBBLE = '[data-testid="msg-container"]'; // UNVERIFIED: needs DOM dump

export const TIMER_STALE_S = 4.0;

export interface SelectorCheck {
  name: string;
  status: 'found' | 'not-found' | 'unverified';
  verified: boolean;
}
