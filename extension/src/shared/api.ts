// REST client for popup / sidepanel / dashboard (EXTENSION_SPEC §6.5).
// All calls carry `Authorization: Bearer <token>`; the token is pasted once
// on the first-run screen (`phathom token` prints it) and kept in
// chrome.storage.local. Never logs or returns the token anywhere else.

export interface ApiError extends Error {
  status: number;
}

export async function getToken(): Promise<string | null> {
  try {
    const v = await chrome.storage.local.get(['phathomToken', 'serverUrl']);
    return (v.phathomToken as string) || null;
  } catch {
    return null;
  }
}

export async function getServerUrl(): Promise<string> {
  try {
    const v = await chrome.storage.local.get(['serverUrl']);
    return (v.serverUrl as string) || 'http://127.0.0.1:8765';
  } catch {
    return 'http://127.0.0.1:8765';
  }
}

export async function setPairing(serverUrl: string, token: string): Promise<void> {
  await chrome.storage.local.set({ serverUrl, phathomToken: token });
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const [base, token] = [await getServerUrl(), await getToken()];
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(base + path, { ...init, headers: { ...headers, ...(init?.headers as object) } });
  } catch (e) {
    const err = new Error(`server unreachable (${base}); is \`phathom serve\` running?`) as ApiError;
    err.status = 0;
    throw err;
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch {
      /* non-JSON error */
    }
    const err = new Error(`${res.status}: ${detail}`) as ApiError;
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => req<{ server: string; db: string; groq_key: string; wa_logged_in: boolean | null; ext_connected?: boolean; version: string }>('/api/health'),
  state: () => req<Record<string, unknown>>('/api/state'),
  patchState: (patch: object) => req<Record<string, unknown>>('/api/state', { method: 'PATCH', body: JSON.stringify(patch) }),
  calls: (params: Record<string, string> = {}) => {
    const q = new URLSearchParams(params).toString();
    return req<{ calls: CallShort[]; next_cursor: string | null }>(`/api/calls${q ? '?' + q : ''}`);
  },
  call: (id: string) => req<CallDetail>(`/api/calls/${encodeURIComponent(id)}`),
  patchCall: (id: string, patch: object) => req<CallShort>(`/api/calls/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteCall: (id: string) => req<{ ok: boolean }>(`/api/calls/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  reprocess: (id: string) => req<CallShort>(`/api/calls/${encodeURIComponent(id)}/reprocess`, { method: 'POST' }),
  ask: (question: string, chat?: string) =>
    req<{ answer: string; citations: { call_id?: string; chat?: string; t?: number; sender?: string; date?: string }[] }>('/api/ask', {
      method: 'POST',
      body: JSON.stringify(chat ? { question, chat } : { question }),
    }),
  contacts: () => req<ContactsFile>('/api/contacts'),
  putContacts: (body: ContactsFile) => req<ContactsFile>('/api/contacts', { method: 'PUT', body: JSON.stringify(body) }),
  brief: () => req<{ brief: string }>('/api/brief'),
  putBrief: (brief: string) => req<{ brief: string }>('/api/brief', { method: 'PUT', body: JSON.stringify({ brief }) }),
  settings: () => req<Record<string, unknown>>('/api/settings'),
  patchSettings: (body: object) => req<Record<string, unknown>>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) }),
  stats: () => req<{ calls_this_week: number; minutes_recorded: number; open_action_items: number }>('/api/stats'),
  actionItems: (open = true) => req<{ items: ActionItem[] }>(`/api/action-items?open=${open ? 'true' : 'false'}`),
  patchActionItem: (id: string, done: boolean) =>
    req<{ id: string; done: boolean }>(`/api/action-items/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ done }) }),
  brain: () => req<{ chats: BrainChat[] }>('/api/brain'),
  brainChat: (chat: string) => req<BrainDetail>(`/api/brain/${encodeURIComponent(chat)}`),
  brainImport: (file: File) => uploadFile('/api/brain/import', file),
  brainRead: (chat: string, messages: ChatMessage[]) =>
    req<{ ok: boolean; added: number }>(`/api/brain/${encodeURIComponent(chat)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),
  brainRebuild: (chat: string) => req<{ ok: boolean }>(`/api/brain/${encodeURIComponent(chat)}/rebuild`, { method: 'POST' }),
  brainDelete: (chat: string) => req<{ ok: boolean }>(`/api/brain/${encodeURIComponent(chat)}`, { method: 'DELETE' }),
  devDump: (body: { label: string; href: string; html: string }) =>
    req<{ ok: boolean; path: string; chars: number }>('/api/dev/dump', { method: 'POST', body: JSON.stringify(body) }),
  diagnostics: () => req<Diagnostics>('/api/diagnostics'),
  audioUrl: (callId: string, track: string) => `/api/calls/${encodeURIComponent(callId)}/audio/${encodeURIComponent(track)}`,
  /** Audio needs the bearer token, which <audio src> can't send: fetch it and
   *  hand back an object URL (caller revokes it). null = no such track. */
  audioObjectUrl: async (path: string): Promise<string | null> => {
    const [base, token] = [await getServerUrl(), await getToken()];
    const res = await fetch(base + path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
    return URL.createObjectURL(await res.blob());
  },
};

async function uploadFile<T>(path: string, file: File): Promise<T> {
  const [base, token] = [await getServerUrl(), await getToken()];
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(base + path, { method: 'POST', headers, body: form });
  if (!res.ok) {
    const err = new Error(`${res.status}: ${res.statusText}`) as ApiError;
    err.status = res.status;
    throw err;
  }
  return (await res.json()) as T;
}

export interface CallShort {
  id: string;
  caller: string;
  started_at: string;
  duration_s?: number | null;
  mode: string;
  status: string;
  title?: string | null;
  summary?: string | null;
  has_message: boolean;
  message?: string | null;
  message_read?: boolean;
  starred?: boolean;
}

export interface CallDetail extends CallShort {
  language?: string | null;
  key_points: string[];
  action_items: { owner: string; item: string; due?: string | null }[];
  message_for_owner?: string | null;
  message_read?: boolean;
  follow_up_needed: boolean;
  notes?: string | null;
  segments: { t_start: number; t_end: number; speaker: string; text: string }[];
  audio: Record<string, string>;
}

export interface ContactsFile {
  policy: string;
  block: string[];
  allow: string[];
  notes: Record<string, string>;
}

export interface ActionItem {
  id: string;
  call_id: string;
  caller: string;
  date: string;
  owner: string;
  item: string;
  due?: string | null;
  done: boolean;
}

export interface BrainChat {
  id: string;
  name: string;
  kind: string;
  updated_at?: string | null;
  msg_count: number;
}

export interface BrainDetail extends BrainChat {
  summary?: string | null;
  people: unknown[];
  facts: unknown[];
  open_loops: unknown[];
  decisions: unknown[];
  timeline: unknown[];
}

export interface ChatMessage {
  ts: string;
  sender: string;
  is_me: boolean;
  text: string;
}

export interface Diagnostics {
  hooks: Record<string, boolean>;
  worklet: string;
  wa: { logged_in: boolean | null };
  caller_source: { mode: string; active: string | null; taps: { label: string; db: number }[] };
  bot_voice: { enabled: boolean; test_tone_heard: boolean | null; path: string };
  selectors: { name: string; status: string; verified: boolean }[];
  tab?: { reporting: boolean; last_seen_s: number | null };
  server: Record<string, unknown>;
}
