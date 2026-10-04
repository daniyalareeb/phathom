// Side panel: live call companion (§7.3) — idle → ringing → in call →
// summarizing → summary, plus a dev Diagnostics section.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { api, CallDetail, ContactsFile, Diagnostics } from '../shared/api';
import { Card, Chip, Empty, ErrorCard, MODE_INFO, ModeChip, modeOf, RULE_LABEL, SkeletonList, TalkShare, errText, fmtClock, ruleFor, speakerClass, speakerName } from './lib';
import './app.css';

interface Line {
  speaker: string;
  text: string;
  t: number;
}
type Phase = 'idle' | 'call' | 'summarizing' | 'done';
type LiveMsg = { _bg?: boolean; t?: string; call_id?: string | null; event?: Record<string, unknown> };

function useLive() {
  const [phase, setPhase] = React.useState<Phase>('idle');
  const [lines, setLines] = React.useState<Line[]>([]);
  const [call, setCall] = React.useState<{ id: string | null; peer: string; kind: string; since: number } | null>(null);
  const [ring, setRing] = React.useState<{ caller: string; at: number } | null>(null);
  const [paused, setPaused] = React.useState(false);
  const [notes, setNotes] = React.useState(0);
  const callId = React.useRef<string | null>(null);

  React.useEffect(() => {
    const onMsg = (msg: LiveMsg) => {
      if (msg._bg !== true || !msg.event) return;
      const ev = msg.event as { type?: string } & Record<string, unknown>;
      if (ev.type === 'ring') {
        setRing({ caller: String(ev.caller || 'Unknown'), at: Date.now() });
      } else if (ev.type === 'call_started') {
        callId.current = msg.call_id ?? null;
        setRing(null);
        setCall({ id: msg.call_id ?? null, peer: String(ev.peer || ''), kind: String(ev.kind || ''), since: Date.now() });
        setLines([]);
        setPaused(false);
        setNotes(0);
        setPhase('call');
      } else if (ev.type === 'transcript') {
        setLines((prev) => [...prev, { speaker: String(ev.speaker), text: String(ev.text), t: Number(ev.t ?? 0) }]);
      } else if (ev.type === 'transcript_paused') {
        setPaused(true);
      } else if (ev.type === 'note_saved') {
        setNotes((n) => n + 1);
      } else if (ev.type === 'call_ended') {
        setRing(null);
        setPhase(callId.current ? 'summarizing' : 'idle');
      } else if (ev.type === 'summary_ready') {
        setPhase('done');
      } else if (ev.type === 'idle') {
        setRing(null);
      }
    };
    chrome.runtime.onMessage.addListener(onMsg);
    return () => chrome.runtime.onMessage.removeListener(onMsg);
  }, []);

  return { phase, setPhase, lines, call, ring, setRing, paused, notes };
}

async function uiAction(action: string, text?: string) {
  await chrome.runtime.sendMessage({ cmd: 'ui_action', action, text }).catch(() => {});
}

function Ringing({ ring, onDismiss }: { ring: { caller: string; at: number }; onDismiss: () => void }) {
  const [info, setInfo] = React.useState<{ contacts: ContactsFile | null; delay: number; answer: string; connected: boolean } | null>(null);
  const [brain, setBrain] = React.useState<{ summary?: string | null; open_loops: { text?: string }[] } | null>(null);
  const [now, setNow] = React.useState(Date.now());
  const [ignored, setIgnored] = React.useState(false);

  React.useEffect(() => {
    Promise.all([api.contacts().catch(() => null), api.state().catch(() => ({}) as Record<string, unknown>)]).then(([c, s]) =>
      setInfo({ contacts: c, delay: Number(s.answer_delay_s ?? 6), answer: String(s.answer ?? 'off'), connected: Boolean(s.connected) }),
    );
    // §8.4: when someone with a brain calls, show its summary + open loops.
    api
      .brain()
      .then(async (b) => {
        const hit = b.chats.find((c) => c.name.trim().toLowerCase() === ring.caller.trim().toLowerCase());
        const d = hit ? await api.brainChat(hit.id).catch(() => null) : null;
        if (d) setBrain({ summary: d.summary, open_loops: (d.open_loops as { text?: string }[]) || [] });
      })
      .catch(() => {});
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [ring]);

  const rule = ruleFor(ring.caller, info?.contacts ?? null);
  const willAnswer = !!info && info.connected && info.answer !== 'off' && rule !== 'block' && !ignored;
  const left = info ? Math.max(0, Math.ceil(info.delay - (now - ring.at) / 1000)) : null;

  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="dot live-dot" style={{ color: 'var(--you)' }} />
        <span className="eyebrow" style={{ margin: 0 }}>Incoming call</span>
      </div>
      <div className="display" style={{ fontSize: 24, fontWeight: 700, margin: '6px 0 6px' }}>{ring.caller}</div>
      <Chip tone={rule === 'block' ? 'danger' : rule === 'allow' ? 'live' : 'neutral'}>{RULE_LABEL[rule]}</Chip>
      <div style={{ marginTop: 12, fontSize: 13 }} className="muted">
        {!info ? '…' : willAnswer ? (
          <>Phathom answers in <strong className="mono" style={{ color: 'var(--ink)' }}>{left}s</strong> ({info.answer === 'talk' ? 'Talk' : 'Listen'})</>
        ) : ignored ? 'Letting it ring. Phathom won’t answer.' : rule === 'block' ? 'Blocked contact. Phathom won’t answer.' : 'Phathom won’t answer this call.'}
      </div>
      {brain?.summary && (
        <div className="callout" style={{ marginTop: 12 }}>
          <div className="eyebrow">What you know about {ring.caller}</div>
          <div style={{ fontSize: 13 }}>{brain.summary}</div>
          {(brain.open_loops || []).slice(0, 2).map((l, i) => (
            <div key={i} className="muted" style={{ fontSize: 13 }}>• {l.text}</div>
          ))}
        </div>
      )}
      {willAnswer && (
        <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
          <button className="btn" style={{ flex: 1 }} onClick={() => { void uiAction('ignore'); setIgnored(true); }}>Let it ring</button>
          <button className="btn btn-primary" style={{ flex: 1 }} onClick={() => void uiAction('answer_now')}>Answer now</button>
        </div>
      )}
      {!willAnswer && info && (
        <button className="btn btn-quiet btn-sm" style={{ marginTop: 10 }} onClick={onDismiss}>Dismiss</button>
      )}
    </Card>
  );
}

function InCall({ call, lines, paused, notes }: { call: { peer: string; kind: string; since: number }; lines: Line[]; paused: boolean; notes: number }) {
  const [now, setNow] = React.useState(Date.now());
  const [note, setNote] = React.useState('');
  const [takenOver, setTakenOver] = React.useState(false);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const stuck = React.useRef(false);

  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  React.useEffect(() => {
    const el = scrollRef.current;
    if (el && !stuck.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const talk = call.kind === 'talk';
  const bot = talk || call.kind === 'listen';
  return (
    <Card style={{ display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Chip tone="danger"><span className="dot live-dot" />REC</Chip>
        <ModeChip mode={call.kind} />
        <span className="mono faint" style={{ marginLeft: 'auto', fontSize: 13 }}>{fmtClock((now - call.since) / 1000)}</span>
      </div>
      <div className="display" style={{ fontSize: 22, fontWeight: 700, lineHeight: 1.15 }}>{call.peer || 'Unknown caller'}</div>

      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight > 60;
        }}
        className="transcript"
        style={{ maxHeight: 340, overflowY: 'auto', margin: '0 -8px' }}
        aria-live="polite"
      >
        {lines.length === 0 && <Empty text="Listening…" hint="The live transcript appears here." />}
        {lines.map((l, i) => (
          <div key={i} className={`tline ${speakerClass(l.speaker)} ${i > 0 && lines[i - 1].speaker === l.speaker ? 'cont' : ''}`} style={{ gridTemplateColumns: '1fr', cursor: 'default', paddingLeft: 8 }}>
            <div className="tbody">
              <div className="who">{speakerName(l.speaker)}</div>
              <div className="what">{l.text}</div>
            </div>
          </div>
        ))}
      </div>
      {paused && <div className="hint" style={{ color: 'var(--warn)' }}>Live transcript paused (Groq rate limit). The full transcript is made after the call.</div>}

      {bot && (
        <div style={{ display: 'flex', gap: 8 }}>
          {talk &&
            (takenOver ? (
              <button className="btn" style={{ flex: 1 }} onClick={() => { void uiAction('hand_back'); setTakenOver(false); }}>Hand back to Phathom</button>
            ) : (
              <button className="btn btn-primary" style={{ flex: 1 }} onClick={() => { void uiAction('take_over'); setTakenOver(true); }}>Take over</button>
            ))}
          <button className="btn btn-danger" style={{ flex: talk ? 'none' : 1 }} onClick={() => void uiAction('hangup')}>Hang up</button>
        </div>
      )}
      {takenOver && <div className="hint">You’re on the line. Phathom is muted but still recording.</div>}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!note.trim()) return;
          void uiAction('note', note.trim());
          setNote('');
        }}
        style={{ display: 'flex', gap: 8 }}
      >
        <input className="input" aria-label="Private note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Private note, added to the summary" />
        <button className="btn" type="submit">Add</button>
      </form>
      {notes > 0 && <div className="hint">{notes === 1 ? '1 note saved' : `${notes} notes saved`}</div>}
    </Card>
  );
}

function Summary({ callId, phase, onClose }: { callId: string; phase: Phase; onClose: () => void }) {
  const [d, setD] = React.useState<CallDetail | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  React.useEffect(() => {
    if (phase !== 'done') return;
    api.call(callId).then(setD).catch((e) => setErr(errText(e)));
  }, [callId, phase]);

  if (phase === 'summarizing' || (!d && !err)) {
    return (
      <Card style={{ display: 'grid', gap: 10 }}>
        <span className="eyebrow" style={{ margin: 0 }}>Call ended</span>
        <div style={{ fontWeight: 600 }}>Writing the summary…</div>
        <SkeletonList rows={3} height={16} />
      </Card>
    );
  }
  if (err || !d) return <ErrorCard message={err || 'no summary'} />;
  return (
    <Card style={{ display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <span className="eyebrow" style={{ margin: 0 }}>Summary</span>
        <ModeChip mode={d.mode} />
      </div>
      <div className="display" style={{ fontSize: 19, fontWeight: 700, lineHeight: 1.2 }}>{d.title || d.caller}</div>
      <TalkShare segments={d.segments} />
      {d.message_for_owner && (
        <div className="callout">
          <div className="eyebrow">Message for you</div>
          <div>{d.message_for_owner}</div>
        </div>
      )}
      {d.summary && <p style={{ margin: 0, fontSize: 13.5 }}>{d.summary}</p>}
      {d.action_items.length > 0 && (
        <div>
          <div className="eyebrow">Action items</div>
          {d.action_items.map((a, i) => (
            <div key={i} style={{ fontSize: 13 }}>• {a.item} <span className="faint">({a.owner === 'owner' || a.owner === 'me' ? 'you' : a.owner})</span></div>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', gap: 8 }}>
        <button className="btn btn-primary" style={{ flex: 1 }} onClick={() => chrome.tabs.create({ url: chrome.runtime.getURL(`app.html#/calls/${d.id}`) })}>Open call</button>
        <button className="btn" onClick={onClose}>Done</button>
      </div>
    </Card>
  );
}

function Idle() {
  const [brief, setBrief] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [state, setState] = React.useState<Record<string, unknown> | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const load = React.useCallback(() => {
    setErr(null);
    api.brief().then((b) => setBrief(b.brief)).catch((e) => setErr(errText(e)));
    api.state().then(setState).catch(() => {});
  }, []);
  React.useEffect(() => {
    load();
  }, [load]);
  if (err) return <ErrorCard message={err} onRetry={load} />;
  const mode = modeOf(state);
  return (
    <>
      <Card tight style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className="dot" style={{ color: mode !== 'off' ? 'var(--you)' : 'var(--ink-3)' }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600 }}>{mode === 'off' ? 'Off' : `${MODE_INFO[mode].label}: waiting for calls`}</div>
          <div className="hint">{mode === 'off' ? 'Pick Copilot or Auto in the popup to start.' : MODE_INFO[mode].help}</div>
        </div>
      </Card>
      <Card>
        <h3 className="eyebrow">Today’s brief</h3>
        <p className="hint" style={{ margin: '-4px 0 8px' }}>What Phathom should know on today’s calls. Re-read on every call.</p>
        {brief === null ? (
          <SkeletonList rows={1} height={70} />
        ) : (
          <textarea
            className="textarea"
            value={brief}
            onChange={(e) => { setBrief(e.target.value); setSaved(false); }}
            onBlur={() => api.putBrief(brief).then(() => setSaved(true)).catch(() => {})}
            rows={4}
            placeholder="e.g. If anyone asks about the report, say it goes out tonight."
          />
        )}
        {saved && <div className="hint" style={{ marginTop: 4 }}>Saved</div>}
      </Card>
    </>
  );
}

function DevDiagnostics() {
  const [open, setOpen] = React.useState(false);
  const [diag, setDiag] = React.useState<Diagnostics | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const load = React.useCallback(async () => {
    setErr(null);
    try {
      setDiag(await api.diagnostics());
    } catch (e) {
      setErr(errText(e));
    }
  }, []);
  return (
    <details className="card card-tight" onToggle={(e) => { const o = (e.target as HTMLDetailsElement).open; setOpen(o); if (o) void load(); }}>
      <summary className="eyebrow" style={{ margin: 0, cursor: 'pointer' }}>Diagnostics</summary>
      {open && (
        <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <button className="btn btn-sm" onClick={() => chrome.runtime.sendMessage({ cmd: 'play_tone' })}>Play test tone</button>
            <button className="btn btn-sm" onClick={() => chrome.runtime.sendMessage({ cmd: 'dump_dom', label: 'sidepanel' })}>Dump DOM</button>
            <button className="btn btn-sm btn-quiet" onClick={load}>Refresh</button>
          </div>
          {err && <ErrorCard message={err} onRetry={load} />}
          {!diag && !err && <SkeletonList rows={2} />}
          {diag && <pre className="mono" style={{ fontSize: 11, whiteSpace: 'pre-wrap', background: 'var(--surface-2)', borderRadius: 8, padding: 8, maxHeight: 260, overflow: 'auto', margin: 0 }}>{JSON.stringify(diag, null, 1)}</pre>}
        </div>
      )}
    </details>
  );
}

function SidePanel() {
  const { phase, setPhase, lines, call, ring, setRing, paused, notes } = useLive();
  return (
    <div style={{ padding: 14, display: 'grid', gap: 12 }}>
      <span className="wordmark" style={{ padding: '2px 2px 0', fontSize: 16 }}>
        <span className="mark" style={{ width: 18, height: 18, borderRadius: 6 }} />
        Phathom
      </span>
      {ring && phase !== 'call' && <Ringing ring={ring} onDismiss={() => setRing(null)} />}
      {phase === 'call' && call && <InCall call={call} lines={lines} paused={paused} notes={notes} />}
      {(phase === 'summarizing' || phase === 'done') && call?.id && <Summary callId={call.id} phase={phase} onClose={() => setPhase('idle')} />}
      {phase === 'idle' && !ring && <Idle />}
      <DevDiagnostics />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<SidePanel />);
