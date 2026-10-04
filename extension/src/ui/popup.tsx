// Popup (360 wide): the quick switch — Connect, Answer for me, Copilot, status.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { api, getToken } from '../shared/api';
import { useServerState, Chip, MODE_INFO, MODE_PATCH, Mode, errText, fmtAgo, fmtDur, modeOf } from './lib';
import './app.css';

type State = Record<string, unknown> | null;

const openPage = (path: string) => chrome.tabs.create({ url: chrome.runtime.getURL(path) });

/** The one control: Off / Copilot / Auto. */
function ModeControl({ state, reload }: { state: State; reload: () => void }) {
  const mode = modeOf(state);
  const wa = state?.wa_logged_in;
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  async function set(m: Mode) {
    if (m === mode) return;
    setBusy(true);
    setErr(null);
    try {
      // Turning on also arms tab capture (needs this click) — §5.7.
      if (mode === 'off') await chrome.runtime.sendMessage({ cmd: 'ensure_capture' }).catch(() => {});
      await api.patchState(MODE_PATCH[m]);
      await reload();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }} role="radiogroup" aria-label="Mode">
        {(['off', 'copilot', 'auto'] as Mode[]).map((m) => {
          const on = m === mode;
          return (
            <button
              key={m}
              role="radio"
              aria-checked={on}
              disabled={busy}
              onClick={() => void set(m)}
              style={{
                padding: '12px 0', borderRadius: 12, cursor: 'pointer', fontWeight: 700, fontSize: 14,
                border: `1px solid ${on ? 'transparent' : 'var(--line-strong)'}`,
                background: on ? (m === 'off' ? 'var(--ink)' : 'var(--accent)') : 'var(--surface)',
                color: on ? '#fff' : 'var(--ink-2)',
              }}
            >
              {MODE_INFO[m].label}
            </button>
          );
        })}
      </div>
      <div className="hint" style={{ marginTop: 8, fontSize: 12.5 }}>{MODE_INFO[mode].help}</div>
      {mode !== 'off' && wa === false && <div style={{ color: 'var(--warn)', fontSize: 12, marginTop: 6 }}>Open WhatsApp Web in this Chrome and scan the QR code.</div>}
      {mode !== 'off' && <div className="hint" style={{ marginTop: 4 }}>Works for calls in WhatsApp Web in this Chrome, not on your phone.</div>}
      {err && <div style={{ color: 'var(--rec)', fontSize: 12, marginTop: 6 }}>{err}</div>}
    </div>
  );
}

function FirstRun({ onDone }: { onDone: () => void }) {
  const [token, setToken] = React.useState('');
  const [url, setUrl] = React.useState('http://127.0.0.1:8765');
  const [err, setErr] = React.useState<string | null>(null);
  async function save() {
    setErr(null);
    if (!token.trim()) return setErr('Paste the token first.');
    try {
      await chrome.storage.local.set({ serverUrl: url.trim(), phathomToken: token.trim() });
      await chrome.runtime.sendMessage({ cmd: 'reconnect' }).catch(() => {});
      await api.health();
      onDone();
    } catch (e) {
      setErr(/401/.test(errText(e)) ? 'That token was rejected. Copy it again from `phathom token`.' : errText(e));
    }
  }
  return (
    <div style={{ width: 360, padding: 18, display: 'grid', gap: 14, boxSizing: 'border-box' }}>
      <Brand />
      <div>
        <h2 className="display" style={{ fontSize: 20, margin: 0 }}>Connect to your server</h2>
        <p className="muted" style={{ margin: '4px 0 0', fontSize: 13 }}>One-time setup. In a terminal in the MyPhathom folder:</p>
      </div>
      <ol style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 6, fontSize: 13 }}>
        <li>Start the server: <code>.venv/bin/phathom serve</code></li>
        <li>Print the token: <code>.venv/bin/phathom token</code></li>
      </ol>
      <label className="field">
        Token
        <input className="input" value={token} onChange={(e) => setToken(e.target.value)} type="password" autoFocus placeholder="Paste the token" />
      </label>
      <details>
        <summary className="hint" style={{ cursor: 'pointer' }}>Server address</summary>
        <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} style={{ marginTop: 6 }} />
      </details>
      {err && <div style={{ color: 'var(--rec)', fontSize: 12 }}>{err}</div>}
      <button onClick={save} className="btn btn-primary btn-block">Save and connect</button>
    </div>
  );
}

function Brand({ right }: { right?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
      <span className="wordmark" style={{ padding: 0, fontSize: 17 }}>
        <span className="mark" />
        Phathom
      </span>
      {right}
    </div>
  );
}

function ServerDown() {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="card" style={{ display: 'grid', gap: 10 }}>
      <div>
        <h2 className="display" style={{ fontSize: 18, margin: 0 }}>Server isn’t running</h2>
        <p className="muted" style={{ margin: '4px 0 0', fontSize: 13 }}>Open a terminal in the MyPhathom folder and run:</p>
      </div>
      <code style={{ padding: '8px 10px', display: 'block' }}>.venv/bin/phathom serve</code>
      <button
        className="btn"
        onClick={() => navigator.clipboard?.writeText('.venv/bin/phathom serve').then(() => setCopied(true)).catch(() => {})}
      >
        {copied ? 'Copied' : 'Copy command'}
      </button>
      <div className="hint">This popup reconnects on its own once the server is up.</div>
    </div>
  );
}

function Popup() {
  const { state, error, reload } = useServerState(3000);
  const [paired, setPaired] = React.useState<boolean | null>(null);
  const [health, setHealth] = React.useState<{ db: string; groq_key: string } | null>(null);
  const [last, setLast] = React.useState<{ id: string; caller: string; duration_s?: number | null; started_at: string; title?: string | null } | null>(null);

  React.useEffect(() => {
    void getToken().then((t) => setPaired(!!t));
  }, []);
  React.useEffect(() => {
    if (!paired || error) return;
    api.health().then(setHealth).catch(() => setHealth(null));
    api.calls({ limit: '1' }).then((r) => setLast(r.calls[0] ?? null)).catch(() => {});
  }, [paired, error]);

  if (paired === null) return <div style={{ width: 360, height: 200 }} />;
  if (!paired) return <FirstRun onDone={() => setPaired(true)} />;

  const wa = state?.wa_logged_in;
  const cur = state?.current_call as { id: string; caller: string } | null | undefined;

  return (
    <div style={{ width: 360, padding: 16, display: 'grid', gap: 14, boxSizing: 'border-box' }}>
      <Brand
        right={
          <button aria-label="Settings" className="btn btn-quiet btn-sm" onClick={() => openPage('app.html#/settings')}>
            Settings
          </button>
        }
      />
      {error && !state ? (
        <ServerDown />
      ) : (
        <>
          {cur && (
            <button className="card card-tight" onClick={() => chrome.sidePanel.open({ windowId: chrome.windows.WINDOW_ID_CURRENT }).catch(() => {})} style={{ textAlign: 'left', cursor: 'pointer', display: 'flex', gap: 10, alignItems: 'center' }}>
              <span className="dot live-dot" style={{ color: 'var(--rec)' }} />
              <span style={{ flex: 1 }}><strong>On a call with {cur.caller}</strong><span className="hint" style={{ display: 'block' }}>Open the live transcript</span></span>
            </button>
          )}
          <ModeControl state={state} reload={reload} />
          <hr className="divider" style={{ margin: 0 }} />
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <Chip tone="live" dot>Server</Chip>
            <Chip tone={health?.db === 'up' ? 'live' : 'warn'} dot>Database</Chip>
            <Chip tone={health?.groq_key === 'set' ? 'live' : 'warn'} dot>{health?.groq_key === 'missing' ? 'Groq key missing' : 'Groq'}</Chip>
            <Chip tone={wa === false ? 'warn' : wa ? 'live' : 'neutral'} dot>{wa === false ? 'WhatsApp logged out' : 'WhatsApp'}</Chip>
          </div>
          {last && (
            <button className="row" style={{ padding: '8px 10px', margin: '0 -10px', width: 'calc(100% + 20px)' }} onClick={() => openPage(`app.html#/calls/${last.id}`)}>
              <span className="avatar">{(last.caller || '?').charAt(0).toUpperCase()}</span>
              <span style={{ minWidth: 0 }}>
                <span className="row-title">{last.caller}</span>
                <span className="row-sub" style={{ display: 'block' }}>{last.title || 'Last call'}</span>
              </span>
              <span className="row-meta mono">{fmtDur(last.duration_s)}<br />{fmtAgo(last.started_at)}</span>
            </button>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={() => openPage('app.html')} className="btn btn-primary" style={{ flex: 1 }}>Open dashboard</button>
            <button onClick={() => chrome.sidePanel.open({ windowId: chrome.windows.WINDOW_ID_CURRENT }).catch(() => {})} className="btn">Side panel</button>
          </div>
        </>
      )}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Popup />);
