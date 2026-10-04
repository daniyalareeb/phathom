// Dashboard (app.html): Home, Calls, Ask, Brain, Contacts, Settings.
// Hash routing (#/calls/<id>?t=42) so notifications and citations deep-link. ⌘K palette.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Brain, House, MessagesSquare, PhoneCall, Search, Settings, Users } from 'lucide-react';
import { api, BrainChat, BrainDetail, CallDetail, CallShort, ContactsFile, Diagnostics } from '../shared/api';
import {
  Card, Chip, Empty, ErrorCard, MODE_INFO, MODE_PATCH, Mode, ModeChip, RULE_LABEL, Rule, modeOf, SectionTitle, SkeletonList, TalkShare, Toast,
  dayLabel, errText, fmtAgo, fmtClock, fmtDur, fmtTime, initial, ruleFor, speakerClass, speakerName,
} from './lib';
import './app.css';

// ---------------------------------------------------------------- routing

type Route = { page: string; arg?: string; query: URLSearchParams };

function parseRoute(): Route {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  const [page, arg] = path.split('/');
  return { page: page || 'home', arg: arg ? decodeURIComponent(arg) : undefined, query: new URLSearchParams(qs || '') };
}

function useRoute(): Route {
  const [r, setR] = React.useState<Route>(parseRoute);
  React.useEffect(() => {
    const on = () => setR(parseRoute());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return r;
}

const go = (p: string) => {
  location.hash = '#/' + p;
};

const ToastCtx = React.createContext<(t: string) => void>(() => {});
const useToast = () => React.useContext(ToastCtx);

/** "20261002_140043_ahmed_uni" → "Ahmed Uni · Oct 2" (citations only carry the id). */
function callLabel(id: string): string {
  const m = id.match(/^(\d{4})(\d{2})(\d{2})_\d{6}_(.+)$/);
  if (!m) return id;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const who = m[4].split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  return `${who} · ${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}`;
}

function PageHead({ title, sub, actions }: { title: string; sub?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
      <div style={{ flex: 1, minWidth: 200 }}>
        <h1 className="page-title">{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {actions}
    </div>
  );
}

function useLoad<T>(fn: () => Promise<T>, deps: React.DependencyList) {
  const [data, setData] = React.useState<T | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const load = React.useCallback(async () => {
    setErr(null);
    try {
      setData(await fn());
    } catch (e) {
      setErr(errText(e));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  React.useEffect(() => {
    void load();
  }, [load]);
  return { data, setData, err, load };
}

// ---------------------------------------------------------------- palette

function Palette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [q, setQ] = React.useState('');
  const [sel, setSel] = React.useState(0);
  React.useEffect(() => {
    if (open) {
      setQ('');
      setSel(0);
    }
  }, [open]);
  if (!open) return null;
  const nav: [string, string][] = [
    ['go:home', 'Home'], ['go:calls', 'Calls'], ['go:ask', 'Ask'], ['go:brain', 'Brain'],
    ['go:contacts', 'Contacts'], ['go:settings', 'Settings'],
  ];
  const items: [string, string, string][] = [
    ...(q.trim() ? ([[`ask:${q}`, `Ask “${q}”`, 'Ask'], [`find:${q}`, `Search calls for “${q}”`, 'Calls']] as [string, string, string][]) : []),
    ['toggle', 'Connect / disconnect', 'Action'],
    ...nav.filter(([, l]) => l.toLowerCase().includes(q.toLowerCase())).map(([a, l]) => [a, `Go to ${l}`, 'Page'] as [string, string, string]),
  ];
  async function run(a: string) {
    if (a === 'toggle') {
      const s = await api.state().catch(() => null);
      if (s) await api.patchState({ connected: !s.connected }).catch(() => {});
    } else if (a.startsWith('go:')) go(a.slice(3));
    else if (a.startsWith('ask:')) go(`ask?q=${encodeURIComponent(a.slice(4))}`);
    else if (a.startsWith('find:')) go(`calls?q=${encodeURIComponent(a.slice(5))}`);
    onClose();
  }
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="palette" role="dialog" aria-label="Command palette">
        <input
          autoFocus
          className="input"
          value={q}
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose();
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(items.length - 1, s + 1)); }
            if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
            if (e.key === 'Enter' && items[sel]) void run(items[sel][0]);
          }}
          placeholder="Ask a question, search calls, or jump to a page"
        />
        <hr className="divider" style={{ margin: '0 0 6px' }} />
        {items.map(([a, label, kind], i) => (
          <button key={a} className="palette-item" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={() => void run(a)}>
            <span>{label}</span>
            <span className="faint" style={{ fontSize: 12 }}>{kind}</span>
          </button>
        ))}
      </div>
    </>
  );
}

// ---------------------------------------------------------------- home

function HomePage({ state }: { state: Record<string, unknown> | null }) {
  const toast = useToast();
  const { data, err, load } = useLoad(async () => {
    const [stats, calls] = await Promise.all([api.stats(), api.calls({ limit: '50' })]);
    return { stats, calls: calls.calls };
  }, []);
  const [read, setRead] = React.useState<Set<string>>(new Set());
  if (err) return <ErrorCard message={err} onRetry={load} />;
  const cur = state?.current_call as { id: string; caller: string; since?: string } | null | undefined;
  const messages = (data?.calls ?? []).filter((c) => c.has_message).sort((a, b) => Number(!!a.message_read || read.has(a.id)) - Number(!!b.message_read || read.has(b.id)));
  const unread = messages.filter((m) => !m.message_read && !read.has(m.id)).length;

  return (
    <>
      <PageHead title="Home" sub={`${MODE_INFO[modeOf(state)].label}: ${MODE_INFO[modeOf(state)].help}`} />
      <div className="stack">
        {cur && (
          <button className="card" onClick={() => chrome.sidePanel?.open({ windowId: chrome.windows.WINDOW_ID_CURRENT }).catch(() => {})} style={{ display: 'flex', alignItems: 'center', gap: 14, textAlign: 'left', cursor: 'pointer', borderColor: 'var(--rec)' }}>
            <Chip tone="danger"><span className="dot live-dot" />Live</Chip>
            <div style={{ flex: 1 }}>
              <div className="display" style={{ fontSize: 18, fontWeight: 700 }}>On a call with {cur.caller}</div>
              <div className="hint">{cur.since ? `Since ${fmtTime(cur.since)} · ` : ''}Open the side panel for the live transcript</div>
            </div>
          </button>
        )}

        <div className="grid-3">
          {!data ? (
            [0, 1, 2].map((i) => <div key={i} className="skeleton" style={{ height: 96 }} />)
          ) : (
            <>
              <Card><div className="eyebrow">Calls this week</div><div className="stat">{data.stats.calls_this_week}</div></Card>
              <Card><div className="eyebrow">Minutes recorded</div><div className="stat">{data.stats.minutes_recorded}</div></Card>
              <Card><div className="eyebrow">Unread messages</div><div className="stat">{unread}</div></Card>
            </>
          )}
        </div>

        <Card>
            <SectionTitle action={unread > 0 ? <Chip tone="accent">{unread} new</Chip> : undefined}>Messages for you</SectionTitle>
            {!data ? <SkeletonList rows={2} /> : messages.length === 0 ? (
              <Empty text="No messages" hint="When Phathom answers for you, callers’ messages land here." />
            ) : (
              <div style={{ display: 'grid', gap: 4, margin: '0 -12px' }}>
                {messages.slice(0, 6).map((m) => {
                  const isRead = m.message_read || read.has(m.id);
                  return (
                    <div key={m.id} className="row" style={{ gridTemplateColumns: '32px 1fr auto', cursor: 'default' }}>
                      <span className="avatar">{initial(m.caller)}</span>
                      <button onClick={() => go(`calls/${m.id}`)} style={{ all: 'unset', cursor: 'pointer', minWidth: 0 }}>
                        <span className="row-title">{!isRead && <span className="dot" style={{ color: 'var(--accent)' }} />}{m.caller}<span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>{fmtAgo(m.started_at)}</span></span>
                        <span className="row-sub" style={{ display: 'block', whiteSpace: 'normal' }}>{m.message}</span>
                      </button>
                      {!isRead && (
                        <button className="btn btn-sm btn-quiet" onClick={async () => { await api.patchCall(m.id, { message_read: true }).catch(() => {}); setRead((s) => new Set(s).add(m.id)); toast('Marked as read'); }}>
                          Mark read
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
        </Card>

        <Card>
          <SectionTitle action={<button className="btn btn-sm btn-quiet" onClick={() => go('calls')}>All calls</button>}>Recent calls</SectionTitle>
          {!data ? <SkeletonList rows={3} /> : data.calls.length === 0 ? (
            <Empty text="No calls yet" hint="Turn on Copilot and call yourself from another phone to try it." />
          ) : (
            <div style={{ margin: '0 -12px' }}>{data.calls.slice(0, 5).map((c) => <CallRow key={c.id} c={c} />)}</div>
          )}
        </Card>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- calls

function CallRow({ c, active }: { c: CallShort; active?: boolean }) {
  return (
    <button className="row" aria-current={active} onClick={() => go(`calls/${c.id}`)}>
      <span className="avatar">{initial(c.caller)}</span>
      <span style={{ minWidth: 0 }}>
        <span className="row-title">
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.caller}</span>
          <ModeChip mode={c.mode} />
          {c.has_message && !c.message_read && <span className="dot" title="Message for you" style={{ color: 'var(--accent)' }} />}
        </span>
        {c.status === 'processing' || c.status === 'live' ? (
          <span className="row-sub" style={{ display: 'block' }}>Writing the summary…</span>
        ) : c.status === 'failed' ? (
          <span className="row-sub" style={{ display: 'block', color: 'var(--rec)' }}>Summary failed. Open the call and press Reprocess.</span>
        ) : (
          <>
            {c.title && <span className="row-sub" style={{ display: 'block', color: 'var(--ink)', fontWeight: 500 }}>{c.title}</span>}
            <span className="row-gist">{c.summary || 'No summary yet'}</span>
          </>
        )}
      </span>
      <span className="row-meta">
        <span className="mono">{fmtDur(c.duration_s)}</span>
        <br />
        {fmtTime(c.started_at)}
      </span>
    </button>
  );
}

function CallsPage({ arg, query }: { arg?: string; query: URLSearchParams }) {
  const [q, setQ] = React.useState(query.get('q') || '');
  const [qLive, setQLive] = React.useState(q);
  const [mode, setMode] = React.useState('');
  const [contact, setContact] = React.useState('');
  const [from, setFrom] = React.useState('');
  const [to, setTo] = React.useState('');
  const [list, setList] = React.useState<CallShort[] | null>(null);
  const [cursor, setCursor] = React.useState<string | null>(null);
  const [err, setErr] = React.useState<string | null>(null);
  const [contacts, setContacts] = React.useState<string[]>([]);

  React.useEffect(() => {
    const id = setTimeout(() => setQ(qLive), 300);
    return () => clearTimeout(id);
  }, [qLive]);

  const params = React.useCallback(
    (extra: Record<string, string> = {}) => {
      const p: Record<string, string> = { limit: '40', ...extra };
      if (q) p.q = q;
      if (mode) p.mode = mode;
      if (contact) p.contact = contact;
      if (from) p.from_ = new Date(from + 'T00:00:00').toISOString();
      if (to) p.to = new Date(to + 'T23:59:59').toISOString();
      return p;
    },
    [q, mode, contact, from, to],
  );

  const load = React.useCallback(async () => {
    setErr(null);
    setList(null);
    try {
      const r = await api.calls(params());
      setList(r.calls);
      setCursor(r.next_cursor);
    } catch (e) {
      setErr(errText(e));
    }
  }, [params]);
  React.useEffect(() => {
    void load();
  }, [load]);
  React.useEffect(() => {
    api.contacts().then((c) => setContacts(Array.from(new Set([...c.allow, ...c.block, ...Object.keys(c.notes)])))).catch(() => {});
  }, []);

  async function more() {
    if (!cursor) return;
    const r = await api.calls(params({ cursor }));
    setList((l) => [...(l ?? []), ...r.calls]);
    setCursor(r.next_cursor);
  }

  const groups: [string, CallShort[]][] = [];
  for (const c of list ?? []) {
    const d = dayLabel(c.started_at);
    const last = groups[groups.length - 1];
    if (last && last[0] === d) last[1].push(c);
    else groups.push([d, [c]]);
  }
  const filtered = !!(q || mode || contact || from || to);

  return (
    <>
      <PageHead title="Calls" sub="Every call Phathom answered or took notes on." />
      <div className={`split ${arg ? 'has-detail' : ''}`}>
        <div className="list-col card" style={{ padding: 10 }}>
          <div style={{ display: 'grid', gap: 8, padding: 4 }}>
            <input className="input" aria-label="Search calls" value={qLive} onChange={(e) => setQLive(e.target.value)} placeholder="Search transcripts and summaries" />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <select className="select" aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="">All modes</option>
                <option value="assistant">Talk</option>
                <option value="notes">Listen</option>
                <option value="copilot">Copilot</option>
              </select>
              <input className="input" aria-label="Contact" list="contact-names" value={contact} onChange={(e) => setContact(e.target.value)} placeholder="Any contact" />
              <datalist id="contact-names">{contacts.map((n) => <option key={n} value={n} />)}</datalist>
              <input className="input" type="date" aria-label="From date" value={from} onChange={(e) => setFrom(e.target.value)} />
              <input className="input" type="date" aria-label="To date" value={to} onChange={(e) => setTo(e.target.value)} />
            </div>
            {filtered && (
              <button className="btn btn-sm btn-quiet" style={{ justifySelf: 'start' }} onClick={() => { setQLive(''); setMode(''); setContact(''); setFrom(''); setTo(''); }}>
                Clear filters
              </button>
            )}
          </div>
          {err ? <ErrorCard message={err} onRetry={load} /> : !list ? <div style={{ padding: 4 }}><SkeletonList rows={6} /></div> : list.length === 0 ? (
            <Empty text={filtered ? 'No calls match' : 'No calls yet'} hint={filtered ? 'Try a different search or clear the filters.' : 'Calls appear here after Phathom answers or Copilot records one.'} />
          ) : (
            <>
              {groups.map(([day, calls]) => (
                <div key={day}>
                  <div className="day-label">{day}</div>
                  {calls.map((c) => <CallRow key={c.id} c={c} active={c.id === arg} />)}
                </div>
              ))}
              {cursor && <button className="btn btn-block" style={{ marginTop: 8 }} onClick={() => void more()}>Load older calls</button>}
            </>
          )}
        </div>
        <div style={{ minWidth: 0 }}>
          {arg ? <CallDetailView id={arg} seek={query.get('t')} onChanged={load} /> : (
            <Card className="desktop-only"><Empty text="Pick a call" hint="Its summary, notes and transcript open here." /></Card>
          )}
        </div>
      </div>
    </>
  );
}

function CallDetailView({ id, seek, onChanged }: { id: string; seek: string | null; onChanged: () => void }) {
  const toast = useToast();
  const { data: d, setData, err, load } = useLoad(() => api.call(id), [id]);
  const [editing, setEditing] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const cited = React.useRef<HTMLDivElement>(null);

  // Ask citations link to a moment (?t=): highlight that line and bring it into view.
  React.useEffect(() => {
    if (d && seek) cited.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [d, seek]);

  // Mark the message read once it has been opened.
  React.useEffect(() => {
    if (d?.message_for_owner && !d.message_read) api.patchCall(d.id, { message_read: true }).then(onChanged).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d?.id]);

  if (err) return <ErrorCard message={err} onRetry={load} />;
  if (!d) return <Card><SkeletonList rows={6} height={22} /></Card>;

  const segs = d.segments;
  const t = seek ? Number(seek) : -1;
  const active = t < 0 ? -1 : segs.findIndex((s, i) => t >= s.t_start && (i === segs.length - 1 || t < segs[i + 1].t_start));

  async function saveTitle(title: string) {
    setEditing(false);
    if (!d || title.trim() === (d.title || '')) return;
    await api.patchCall(d.id, { title: title.trim() }).catch(() => {});
    setData({ ...d, title: title.trim() });
    onChanged();
  }

  return (
    <div className="stack">
      <Card>
        <button className="btn btn-sm btn-quiet mobile-only" onClick={() => go('calls')} style={{ marginBottom: 8 }}>← All calls</button>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
          <ModeChip mode={d.mode} />
          {d.status !== 'done' && <Chip tone={d.status === 'failed' ? 'danger' : 'warn'}>{d.status === 'processing' ? 'Summarising' : d.status}</Chip>}
          <span className="faint" style={{ fontSize: 12.5, marginLeft: 'auto' }}>
            {new Date(d.started_at).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}
          </span>
        </div>
        {editing ? (
          <input
            autoFocus
            className="input display"
            defaultValue={d.title || ''}
            style={{ fontSize: 22, fontWeight: 700 }}
            onBlur={(e) => void saveTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void saveTitle((e.target as HTMLInputElement).value);
              if (e.key === 'Escape') setEditing(false);
            }}
          />
        ) : (
          <h2 className="display" title="Click to rename" onClick={() => setEditing(true)} style={{ fontSize: 24, fontWeight: 700, margin: 0, cursor: 'text', letterSpacing: '-0.02em', lineHeight: 1.2 }}>
            {d.title || 'Untitled call'}
          </h2>
        )}
        <div className="muted" style={{ marginTop: 4 }}>
          {d.caller} · <span className="mono">{fmtDur(d.duration_s)}</span>{d.language ? ` · ${d.language}` : ''}
        </div>
        {segs.length > 0 && <div style={{ marginTop: 14 }}><TalkShare segments={segs} /></div>}
      </Card>

      {d.message_for_owner && (
        <div className="callout">
          <div className="eyebrow">Message for you</div>
          <div style={{ fontSize: 15 }}>{d.message_for_owner}</div>
        </div>
      )}

      {(d.summary || d.key_points.length > 0 || d.action_items.length > 0) && (
        <Card>
          {d.summary && (
            <>
              <SectionTitle>Summary</SectionTitle>
              <p style={{ margin: 0, fontSize: 14.5 }}>{d.summary}</p>
            </>
          )}
          {d.key_points.length > 0 && (
            <>
              <hr className="divider" />
              <SectionTitle>Key points</SectionTitle>
              <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 4 }}>{d.key_points.map((k, i) => <li key={i}>{k}</li>)}</ul>
            </>
          )}
          {d.action_items.length > 0 && (
            <>
              <hr className="divider" />
              <SectionTitle>Action items</SectionTitle>
              <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 6 }}>
                {d.action_items.map((a, i) => (
                  <li key={i}>
                    {a.item}
                    <span className="faint" style={{ fontSize: 12 }}>
                      {' '}· {a.owner === 'owner' || a.owner === 'me' ? 'You' : a.owner}{a.due ? ` · due ${a.due}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Card>
      )}

      <Card>
        <SectionTitle>Transcript</SectionTitle>
        {segs.length === 0 ? (
          <Empty text="No transcript" hint={d.status === 'processing' ? 'It’s being written now.' : 'Reprocess the call to try again.'} />
        ) : (
          <div className="transcript" style={{ margin: '0 -8px' }}>
            {segs.map((s, i) => (
              <div
                key={i}
                ref={i === active ? cited : undefined}
                className={`tline ${speakerClass(s.speaker)} ${i > 0 && segs[i - 1].speaker === s.speaker ? 'cont' : ''}`}
                aria-current={i === active}
                style={{ cursor: 'default' }}
              >
                <span className="ts">{fmtClock(s.t_start)}</span>
                <span className="tbody">
                  <span className="who" style={{ display: i > 0 && segs[i - 1].speaker === s.speaker ? 'none' : 'block' }}>{speakerName(s.speaker)}</span>
                  <span className="what" style={{ display: 'block' }}>{s.text}</span>
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          className="btn"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api.reprocess(d.id);
              toast('Reprocessing started');
              await load();
            } catch (e) {
              toast(errText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          Reprocess
        </button>
        <span style={{ flex: 1 }} />
        <button
          className="btn btn-quiet"
          style={{ color: 'var(--rec)' }}
          onClick={async () => {
            if (!confirm(`Delete this call with ${d.caller}? The recording and transcript are removed for good.`)) return;
            await api.deleteCall(d.id).catch(() => {});
            toast('Call deleted');
            onChanged();
            go('calls');
          }}
        >
          Delete call
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- ask

type Qa = { q: string; a?: string; err?: string; citations?: { call_id?: string; chat?: string; t?: number; sender?: string; date?: string }[] };

function AskPage({ query, chat }: { query: URLSearchParams; chat?: string }) {
  const [q, setQ] = React.useState('');
  const [log, setLog] = React.useState<Qa[]>([]);
  const [busy, setBusy] = React.useState(false);
  const endRef = React.useRef<HTMLDivElement>(null);

  const ask = React.useCallback(async (question: string) => {
    const qq = question.trim();
    if (!qq) return;
    setQ('');
    setBusy(true);
    setLog((l) => [...l, { q: qq }]);
    try {
      const r = await api.ask(qq, chat);
      setLog((l) => l.map((x, i) => (i === l.length - 1 ? { ...x, a: r.answer, citations: r.citations } : x)));
    } catch (e) {
      setLog((l) => l.map((x, i) => (i === l.length - 1 ? { ...x, err: errText(e) } : x)));
    } finally {
      setBusy(false);
    }
  }, [chat]);

  React.useEffect(() => {
    const pre = query.get('q');
    if (pre) void ask(pre);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  React.useEffect(() => endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }), [log]);

  const suggestions = ['What did Ahmed want?', 'Any action items for me this week?', 'What did Ammi say last time?'];
  return (
    <>
      <PageHead title="Ask" sub={chat ? `Questions about the ${chat} chat.` : 'Ask anything about your calls and imported chats. Answers cite where they came from.'} />
      <div className="stack" style={{ maxWidth: 760 }}>
        {log.length === 0 && (
          <Card>
            <SectionTitle>Try asking</SectionTitle>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {suggestions.map((s) => <button key={s} className="btn btn-sm" onClick={() => void ask(s)}>{s}</button>)}
            </div>
          </Card>
        )}
        {log.map((x, i) => (
          <div key={i} style={{ display: 'grid', gap: 8 }}>
            <div style={{ justifySelf: 'end', background: 'var(--ink)', color: '#fff', borderRadius: '14px 14px 4px 14px', padding: '9px 14px', maxWidth: '80%' }}>{x.q}</div>
            {x.err ? <ErrorCard message={x.err} onRetry={() => void ask(x.q)} /> : !x.a ? (
              <Card><SkeletonList rows={2} height={16} /></Card>
            ) : (
              <Card>
                <p style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 14.5 }}>{x.a}</p>
                {!!x.citations?.length && (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 12 }}>
                    {dedupeCitations(x.citations).map((c, j) =>
                      c.call_id ? (
                        <button key={j} className="btn btn-sm" onClick={() => go(`calls/${c.call_id}${c.t != null ? `?t=${Math.floor(c.t)}` : ''}`)}>
                          <PhoneCall size={12} /> {callLabel(c.call_id)}{c.t != null && <span className="mono faint">{fmtClock(c.t)}</span>}
                        </button>
                      ) : (
                        <button key={j} className="btn btn-sm" onClick={() => go(`brain/${c.chat}`)}>
                          <MessagesSquare size={12} /> {c.chat}{c.date ? <span className="faint">{c.date}</span> : null}
                        </button>
                      ),
                    )}
                  </div>
                )}
              </Card>
            )}
          </div>
        ))}
        <div ref={endRef} />
        <form
          onSubmit={(e) => { e.preventDefault(); void ask(q); }}
          className="card card-tight"
          style={{ display: 'flex', gap: 8, position: 'sticky', bottom: 16, boxShadow: 'var(--shadow-pop)' }}
        >
          <input className="input" aria-label="Your question" autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about your calls…" style={{ border: 0, boxShadow: 'none' }} />
          <button className="btn btn-primary" disabled={busy || !q.trim()} type="submit">Ask</button>
        </form>
      </div>
    </>
  );
}

function dedupeCitations<T extends { call_id?: string; chat?: string; t?: number }>(cs: T[]): T[] {
  const seen = new Set<string>();
  return cs.filter((c) => {
    const k = c.call_id ? `${c.call_id}@${c.t ?? ''}` : `chat:${c.chat}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).slice(0, 8);
}

// ---------------------------------------------------------------- brain

function BrainPage() {
  const toast = useToast();
  const { data: chats, err, load } = useLoad(async () => (await api.brain()).chats, []);
  const [busy, setBusy] = React.useState(false);
  const [importErr, setImportErr] = React.useState<string | null>(null);
  async function onFile(f: File | undefined) {
    if (!f) return;
    setBusy(true);
    setImportErr(null);
    try {
      const r = await api.brainImport(f) as { name?: string; added?: number };
      toast(`Imported ${r.added ?? 0} messages${r.name ? ` from ${r.name}` : ''}`);
      await load();
    } catch (e) {
      setImportErr(errText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHead title="Brain" sub="What Phathom knows from your WhatsApp chats. It’s used when that person calls, and in Ask." />
      <div className="stack">
        <Card style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <div style={{ fontWeight: 600 }}>Import a chat</div>
            <div className="hint">On your phone: open the chat → ⋮ → More → Export chat → Without media. Upload the .txt or .zip here.</div>
            {importErr && <div style={{ color: 'var(--rec)', fontSize: 12, marginTop: 4 }}>{importErr}</div>}
          </div>
          <label className="btn btn-primary" style={{ cursor: busy ? 'default' : 'pointer' }}>
            {busy ? 'Importing…' : 'Choose file'}
            <input type="file" accept=".txt,.zip" disabled={busy} hidden onChange={(e) => { void onFile(e.target.files?.[0]); e.target.value = ''; }} />
          </label>
        </Card>
        {err ? <ErrorCard message={err} onRetry={load} /> : !chats ? <SkeletonList rows={3} /> : chats.length === 0 ? (
          <Card><Empty text="No chats yet" hint="Import an exported chat to give Phathom context about that person." /></Card>
        ) : (
          <div className="grid-2">
            {chats.map((c: BrainChat) => (
              <button key={c.id} className="card" onClick={() => go(`brain/${c.id}`)} style={{ textAlign: 'left', cursor: 'pointer', display: 'flex', gap: 12, alignItems: 'center' }}>
                <span className="avatar" style={{ background: 'var(--you-soft)', color: 'var(--you)' }}>{initial(c.name)}</span>
                <span style={{ flex: 1 }}>
                  <span style={{ fontWeight: 600, display: 'block' }}>{c.name}</span>
                  <span className="hint"><span className="mono">{c.msg_count}</span> messages · updated {fmtAgo(c.updated_at)}</span>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

function BrainDetailPage({ chat }: { chat: string }) {
  const toast = useToast();
  const { data: d, err, load } = useLoad<BrainDetail>(() => api.brainChat(chat), [chat]);
  const [rebuilding, setRebuilding] = React.useState(false);
  if (err) return <ErrorCard message={err} onRetry={load} />;
  if (!d) return <SkeletonList rows={4} />;
  const list = (k: keyof BrainDetail) => ((d[k] as { text?: string; item?: string }[] | undefined) || []).map((x) => (typeof x === 'string' ? x : x.text || x.item || JSON.stringify(x)));
  const sections: [keyof BrainDetail, string][] = [['facts', 'Facts'], ['open_loops', 'Open loops'], ['decisions', 'Decisions']];
  return (
    <>
      <button className="btn btn-sm btn-quiet" onClick={() => go('brain')} style={{ marginBottom: 10 }}>← Brain</button>
      <PageHead
        title={d.name}
        sub={<><span className="mono">{d.msg_count}</span> messages · updated {fmtAgo(d.updated_at)}</>}
        actions={
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={() => go(`ask?chat=${encodeURIComponent(chat)}`)}>Ask about this chat</button>
            <button className="btn" disabled={rebuilding} onClick={async () => { setRebuilding(true); await api.brainRebuild(chat).catch(() => {}); toast('Rebuilding… this takes a minute'); setRebuilding(false); }}>Rebuild</button>
            <button className="btn btn-quiet" style={{ color: 'var(--rec)' }} onClick={async () => { if (!confirm(`Delete the ${d.name} chat and everything learned from it?`)) return; await api.brainDelete(chat).catch(() => {}); toast('Chat deleted'); go('brain'); }}>Delete</button>
          </div>
        }
      />
      <div className="stack">
        <Card>
          <SectionTitle>Summary</SectionTitle>
          {d.summary ? <p style={{ margin: 0, fontSize: 14.5 }}>{d.summary}</p> : <Empty text="Still building" hint="Come back in a minute, or press Rebuild." />}
        </Card>
        <div className="grid-3" style={{ alignItems: 'start' }}>
          {sections.map(([k, label]) => (
            <Card key={String(k)}>
              <SectionTitle>{label}</SectionTitle>
              {list(k).length === 0 ? <div className="hint">None yet.</div> : (
                <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 6, fontSize: 13.5 }}>{list(k).map((t, i) => <li key={i}>{t}</li>)}</ul>
              )}
            </Card>
          ))}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- contacts

function ContactsPage() {
  const toast = useToast();
  const { data: c, setData, err, load } = useLoad<ContactsFile>(() => api.contacts(), []);
  const [edit, setEdit] = React.useState<{ orig: string | null; name: string; rule: Rule; notes: string } | null>(null);
  const [saveErr, setSaveErr] = React.useState<string | null>(null);

  async function save(next: ContactsFile, msg: string) {
    setSaveErr(null);
    try {
      setData(await api.putContacts(next));
      toast(msg);
      return true;
    } catch (e) {
      setSaveErr(errText(e));
      return false;
    }
  }
  if (err) return <ErrorCard message={err} onRetry={load} />;
  if (!c) return <SkeletonList rows={4} />;

  const names = Array.from(new Set([...c.allow, ...c.block, ...Object.keys(c.notes)])).sort((a, b) => a.localeCompare(b));
  const without = (n: string): ContactsFile => {
    const notes = { ...c.notes };
    delete notes[n];
    return { ...c, allow: c.allow.filter((x) => x !== n), block: c.block.filter((x) => x !== n), notes };
  };

  async function submit() {
    if (!edit || !c) return;
    const name = edit.name.trim();
    if (!name) return setSaveErr('Enter the name exactly as WhatsApp shows it.');
    const base = without(edit.orig ?? name);
    const next = { ...base, allow: [...base.allow], block: [...base.block], notes: { ...base.notes } };
    if (edit.rule === 'allow') next.allow.push(name);
    if (edit.rule === 'block') next.block.push(name);
    if (edit.notes.trim()) next.notes[name] = edit.notes.trim();
    else if (edit.rule === 'default') next.notes[name] = '';
    if (await save(next, edit.orig ? 'Contact saved' : 'Contact added')) setEdit(null);
  }

  return (
    <>
      <PageHead
        title="Contacts"
        sub="Who Phathom answers, and what it should know about them. Saved to contacts.yaml."
        actions={<button className="btn btn-primary" onClick={() => { setSaveErr(null); setEdit({ orig: null, name: '', rule: 'default', notes: '' }); }}>Add contact</button>}
      />
      <div className="stack">
        <Card style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div style={{ fontWeight: 600 }}>Who can Phathom answer?</div>
            <div className="hint">{c.policy === 'allowlist' ? 'Only contacts marked Allowed.' : 'Everyone except contacts marked Blocked.'}</div>
          </div>
          <div className="segmented" role="group" aria-label="Answer policy" style={{ width: 300 }}>
            <button aria-pressed={c.policy === 'all'} onClick={() => void save({ ...c, policy: 'all' }, 'Answering everyone not blocked')}>Everyone not blocked</button>
            <button aria-pressed={c.policy === 'allowlist'} onClick={() => void save({ ...c, policy: 'allowlist' }, 'Answering allowed contacts only')}>Allowed only</button>
          </div>
        </Card>
        {saveErr && !edit && <ErrorCard message={saveErr} />}
        <Card style={{ padding: 8 }}>
          {names.length === 0 ? (
            <Empty text="No contacts yet" hint="Add people to allow or block them, or to give Phathom notes about them." />
          ) : (
            names.map((n) => {
              const rule = ruleFor(n, c);
              return (
                <button key={n} className="row" onClick={() => { setSaveErr(null); setEdit({ orig: n, name: n, rule, notes: c.notes[n] || '' }); }}>
                  <span className="avatar">{initial(n)}</span>
                  <span style={{ minWidth: 0 }}>
                    <span className="row-title">{n}</span>
                    <span className="row-sub" style={{ display: 'block' }}>{c.notes[n] || <span className="faint">No notes</span>}</span>
                  </span>
                  <Chip tone={rule === 'block' ? 'danger' : rule === 'allow' ? 'live' : 'neutral'}>{RULE_LABEL[rule]}</Chip>
                </button>
              );
            })
          )}
        </Card>
      </div>

      {edit && (
        <>
          <div className="scrim" onClick={() => setEdit(null)} />
          <div className="drawer" role="dialog" aria-label={edit.orig ? 'Edit contact' : 'Add contact'}>
            <h2 className="display" style={{ margin: 0, fontSize: 22 }}>{edit.orig ? 'Edit contact' : 'Add contact'}</h2>
            <label className="field">
              Name
              <input className="input" autoFocus value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} placeholder="Exactly as WhatsApp shows it" />
            </label>
            <div className="field">
              Rule
              <div className="segmented" role="group" aria-label="Rule">
                {(['allow', 'default', 'block'] as Rule[]).map((r) => (
                  <button key={r} aria-pressed={edit.rule === r} onClick={() => setEdit({ ...edit, rule: r })}>{r === 'allow' ? 'Allow' : r === 'block' ? 'Block' : 'Default'}</button>
                ))}
              </div>
              <span className="hint">{edit.rule === 'block' ? 'Phathom never answers this person.' : edit.rule === 'allow' ? 'Phathom answers, even when only allowed contacts are answered.' : 'Follows the policy above.'}</span>
            </div>
            <label className="field">
              Notes for Phathom
              <textarea className="textarea" rows={5} value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} placeholder="e.g. My manager. Speaks Urdu. Always take a message." />
            </label>
            {saveErr && <div style={{ color: 'var(--rec)', fontSize: 12 }}>{saveErr}</div>}
            <div style={{ display: 'flex', gap: 8, marginTop: 'auto' }}>
              {edit.orig && (
                <button className="btn btn-quiet" style={{ color: 'var(--rec)' }} onClick={async () => { if (await save(without(edit.orig!), 'Contact removed')) setEdit(null); }}>Remove</button>
              )}
              <span style={{ flex: 1 }} />
              <button className="btn" onClick={() => setEdit(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={() => void submit()}>Save</button>
            </div>
          </div>
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------- settings

function SettingsPage() {
  const toast = useToast();
  const { data: s, setData: setS, err, load } = useLoad(() => api.settings(), []);
  const [brief, setBrief] = React.useState<string | null>(null);
  const [diag, setDiag] = React.useState<Diagnostics | null>(null);
  const [groqKey, setGroqKey] = React.useState('');
  const [wiping, setWiping] = React.useState(false);

  const loadDiag = React.useCallback(() => api.diagnostics().then(setDiag).catch(() => {}), []);
  React.useEffect(() => {
    api.brief().then((b) => setBrief(b.brief)).catch(() => {});
    void loadDiag();
  }, [loadDiag]);

  async function patch(p: Record<string, unknown>) {
    try {
      setS(await api.patchSettings(p));
      toast('Saved');
    } catch (e) {
      toast(errText(e));
    }
  }
  if (err) return <ErrorCard message={err} onRetry={load} />;
  if (!s) return <SkeletonList rows={6} />;

  const num = (k: string, label: string, unit: string, hint?: string) => (
    <label className="field" style={{ gridTemplateColumns: '1fr 160px', alignItems: 'center', columnGap: 16 }}>
      <span>
        <span style={{ color: 'var(--ink)', fontSize: 13.5 }}>{label}</span>
        {hint && <span className="hint" style={{ display: 'block' }}>{hint}</span>}
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input className="input mono" type="number" min={0} key={String(s[k])} defaultValue={String(s[k] ?? '')} onBlur={(e) => { if (e.target.value !== String(s[k])) void patch({ [k]: Number(e.target.value) }); }} />
        <span className="hint">{unit}</span>
      </span>
    </label>
  );
  const toggle = (k: string, label: string, hint?: string, dflt = true) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
      <span style={{ flex: 1 }}>
        <span style={{ fontSize: 13.5, fontWeight: 600 }}>{label}</span>
        {hint && <span className="hint" style={{ display: 'block' }}>{hint}</span>}
      </span>
      <button className="switch" role="switch" aria-label={label} aria-checked={Boolean(s[k] ?? dflt)} onClick={() => void patch({ [k]: !(s[k] ?? dflt) })} />
    </div>
  );
  const section = (title: string, children: React.ReactNode, sub?: string) => (
    <Card>
      <h2 className="display" style={{ fontSize: 17, margin: 0 }}>{title}</h2>
      {sub && <p className="hint" style={{ margin: '2px 0 0' }}>{sub}</p>}
      <div style={{ display: 'grid', gap: 16, marginTop: 16 }}>{children}</div>
    </Card>
  );

  return (
    <>
      <PageHead title="Settings" />
      <div className="stack" style={{ maxWidth: 780 }}>
        {section('Answering', <>
          {num('answer_delay_s', 'Answer after', 'seconds', 'How long a call rings before Phathom picks up.')}
          {toggle('disclosure', 'Say it’s an assistant', 'Phathom tells callers it’s an AI assistant before anything else.')}
          {toggle('talk_mode_enabled', 'Talk to callers in Auto', 'Off: in Auto, Phathom only plays the disclosure and takes a message.')}
          {toggle('side_panel_auto', 'Open the side panel when a call rings')}
        </>)}

        {section('Calls', <>
          {num('max_call_s', 'Longest call Phathom handles', 'seconds')}
          {num('silence_hangup_s', 'Hang up after silence', 'seconds', 'When nobody has spoken for this long.')}
          {num('owner_call_max_s', 'Longest Copilot recording', 'seconds', 'For calls you take yourself.')}
          {toggle('live_transcript', 'Live transcript', 'Shows the call as text while it happens. Uses more Groq quota.')}
        </>)}

        {section('Voice', <>
          <label className="field">English voice<input className="input" key={String(s.tts_voice_en)} defaultValue={String(s.tts_voice_en ?? '')} onBlur={(e) => { if (e.target.value !== s.tts_voice_en) void patch({ tts_voice_en: e.target.value }); }} /></label>
          <label className="field">Urdu voice<input className="input" key={String(s.tts_voice_ur)} defaultValue={String(s.tts_voice_ur ?? '')} onBlur={(e) => { if (e.target.value !== s.tts_voice_ur) void patch({ tts_voice_ur: e.target.value }); }} /></label>
          <div><button className="btn" onClick={() => chrome.runtime.sendMessage({ cmd: 'play_tone' }).catch(() => {})}>Play test tone into the call</button></div>
        </>, 'Edge TTS voice names, e.g. en-US-AndrewNeural.')}

        {section('Today’s brief', brief === null ? <SkeletonList rows={1} height={80} /> : (
          <textarea className="textarea" rows={4} value={brief} onChange={(e) => setBrief(e.target.value)} onBlur={() => api.putBrief(brief).then(() => toast('Brief saved')).catch(() => {})} placeholder="e.g. If anyone asks about the report, say it goes out tonight." />
        ), 'What Phathom should know on today’s calls. Re-read on every call.')}

        {section('Connection', <>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Chip tone="live" dot>Server running</Chip>
            <Chip tone={s.groq_key === 'set' ? 'live' : 'warn'} dot>Groq key {s.groq_key === 'set' ? 'set' : 'missing'}</Chip>
          </div>
          <form style={{ display: 'flex', gap: 8 }} onSubmit={async (e) => { e.preventDefault(); if (!groqKey) return; await patch({ groq_key: groqKey }); setGroqKey(''); }}>
            <input className="input" aria-label="Groq API key" type="password" value={groqKey} onChange={(e) => setGroqKey(e.target.value)} placeholder="Paste a new Groq API key" />
            <button className="btn btn-primary" type="submit" disabled={!groqKey}>Save key</button>
          </form>
          <div><button className="btn btn-quiet btn-sm" onClick={async () => { await chrome.storage.local.remove(['phathomToken']); toast('Token cleared. Open the popup to pair again.'); }}>Forget server token</button></div>
        </>, 'The key is written to .env and never shown again.')}

        {section('Diagnostics', !diag ? <SkeletonList rows={3} height={20} /> : (
          <div style={{ display: 'grid', gap: 12, fontSize: 13 }}>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="muted" style={{ width: 110 }}>Audio hooks</span>
              {Object.entries(diag.hooks).map(([k, v]) => <Chip key={k} tone={v ? 'live' : 'warn'} dot>{k}</Chip>)}
              <Chip>{diag.worklet}</Chip>
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="muted" style={{ width: 110 }}>WhatsApp tab</span>
              <Chip tone={diag.tab?.reporting ? 'live' : 'warn'} dot>{diag.tab?.reporting ? 'Reporting' : 'Not reporting'}</Chip>
              {!diag.tab?.reporting && <span className="hint">Open WhatsApp Web in this Chrome and reload that tab.</span>}
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="muted" style={{ width: 110 }}>WhatsApp</span>
              <Chip tone={diag.wa.logged_in ? 'live' : 'warn'} dot>{diag.wa.logged_in ? 'Logged in' : 'Logged out'}</Chip>
            </div>
            <label style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="muted" style={{ width: 110 }}>Caller voice</span>
              <select className="select" style={{ width: 'auto' }} value={String(s.caller_source ?? 'auto')} onChange={(e) => void patch({ caller_source: e.target.value })}>
                <option value="auto">Auto (recommended)</option>
                <option value="webrtc">WebRTC tap</option>
                <option value="speaker_tap">Speaker tap</option>
                <option value="tab_capture">Tab capture</option>
              </select>
              <span className="hint">Active: {diag.caller_source.active ?? 'none yet'}</span>
            </label>
            {diag.caller_source.taps.length > 0 && (
              <div style={{ display: 'grid', gap: 4, paddingLeft: 116 }}>
                {diag.caller_source.taps.map((t) => (
                  <div key={t.label} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <code style={{ width: 150 }}>{t.label}</code>
                    <meter value={Math.max(0, Math.min(1, (t.db + 90) / 90))} style={{ width: 140 }} />
                    <span className="mono faint">{t.db} dB</span>
                  </div>
                ))}
              </div>
            )}
            <label className="check" style={{ paddingLeft: 116 }}>
              <input type="checkbox" checked={diag.bot_voice.test_tone_heard === true} onChange={async (e) => { await api.patchSettings({ test_tone_heard: e.target.checked }).catch(() => {}); void loadDiag(); }} />
              <span>I heard the test tone on the phone <span className="hint" style={{ display: 'block' }}>Bot voice path: {diag.bot_voice.path}</span></span>
            </label>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
              <span className="muted" style={{ width: 110 }}>Selectors</span>
              {diag.selectors.map((x) => <Chip key={x.name} tone={!x.verified || x.status === 'unknown' ? 'neutral' : x.status === 'found' ? 'live' : 'warn'}>{x.name}: {x.status}</Chip>)}
              <span className="hint" style={{ flexBasis: '100%', paddingLeft: 116 }}>Live from the WhatsApp tab. Call-screen items show “not-found” unless a call is on screen.</span>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-sm" onClick={() => chrome.runtime.sendMessage({ cmd: 'dump_dom', label: 'diagnostics' }).then(() => toast('DOM dump saved to data/dumps')).catch(() => {})}>Dump DOM</button>
              <button className="btn btn-sm" onClick={() => navigator.clipboard?.writeText(JSON.stringify(diag, null, 2)).then(() => toast('Diagnostics copied')).catch(() => {})}>Copy diagnostics</button>
              <button className="btn btn-sm btn-quiet" onClick={() => void loadDiag()}>Refresh</button>
            </div>
          </div>
        ), 'For testing and troubleshooting call audio.')}

        {section('Privacy', <>
          <div className="hint" style={{ fontSize: 13 }}>Recordings aren’t kept. Call audio is used only to write the transcript and summary, then deleted. If a summary fails, the audio is kept until Reprocess succeeds.</div>
          <div>
            <button
              className="btn btn-danger"
              disabled={wiping}
              onClick={async () => {
                if (!confirm('Delete every call, transcript and recording? This can’t be undone.')) return;
                setWiping(true);
                try {
                  let n = 0;
                  for (;;) {
                    const r = await api.calls({ limit: '100' });
                    if (!r.calls.length) break;
                    for (const c of r.calls) { await api.deleteCall(c.id); n++; }
                  }
                  toast(`Deleted ${n} calls`);
                } catch (e) {
                  toast(errText(e));
                } finally {
                  setWiping(false);
                }
              }}
            >
              {wiping ? 'Deleting…' : 'Delete all calls'}
            </button>
          </div>
        </>)}

        {section('Command line', (
          <div style={{ display: 'grid', gap: 6 }}>
            {[
              ['phathom serve', 'Start Phathom. Ctrl+C stops it.'],
              ['phathom token', 'Print the token for the popup'],
              ['phathom status', 'Mode, current call, last 3 calls'],
              ['phathom calls / show <id> / ask "…"', 'Calls and Ask, in the terminal (server stopped)'],
              ['phathom brief "…"', 'Set today’s brief'],
              ['phathom process <wav>', 'Process a recording by hand'],
            ].map(([cmd, what]) => (
              <div key={cmd} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 300px) 1fr', gap: 12, alignItems: 'baseline' }}>
                <code>{cmd}</code>
                <span className="hint">{what}</span>
              </div>
            ))}
          </div>
        ), 'Run from the MyPhathom folder as .venv/bin/phathom …')}
      </div>
    </>
  );
}

// ---------------------------------------------------------------- shell

function Dashboard() {
  const route = useRoute();
  const [state, setState] = React.useState<Record<string, unknown> | null>(null);
  const [down, setDown] = React.useState(false);
  const [palette, setPalette] = React.useState(false);
  const [toast, setToast] = React.useState<string | null>(null);

  React.useEffect(() => {
    const poll = () => {
      api.state().then((s) => { setState(s); setDown(false); }).catch(() => setDown(true));
    };
    poll();
    const id = setInterval(poll, 5000);
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPalette((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => { clearInterval(id); window.removeEventListener('keydown', onKey); };
  }, []);

  const nav: [string, string, React.ComponentType<{ size?: number }>][] = [
    ['home', 'Home', House], ['calls', 'Calls', PhoneCall], ['ask', 'Ask', Search], ['brain', 'Brain', Brain],
    ['contacts', 'Contacts', Users], ['settings', 'Settings', Settings],
  ];
  const mode = modeOf(state);
  const cur = state?.current_call as { caller: string } | null | undefined;
  async function setMode(m: Mode) {
    if (m === mode) return;
    if (mode === 'off') await chrome.runtime.sendMessage({ cmd: 'ensure_capture' }).catch(() => {});
    const s = await api.patchState(MODE_PATCH[m]).catch(() => null);
    if (s) setState(s);
  }

  return (
    <ToastCtx.Provider value={setToast}>
      <div className="shell">
        <nav className="rail" aria-label="Main">
          <div className="wordmark"><span className="mark" />Phathom</div>
          {nav.map(([p, label, Icon]) => (
            <button key={p} className="navbtn" aria-current={route.page === p ? 'page' : undefined} onClick={() => go(p)}>
              <Icon size={16} />
              {label}
            </button>
          ))}
          <div className="rail-foot">
            <button className="btn btn-sm" onClick={() => setPalette(true)} style={{ justifyContent: 'space-between' }}>
              Search <span className="kbd">Ctrl K</span>
            </button>
          </div>
        </nav>
        <div style={{ minWidth: 0 }}>
          <header className="topbar">
            {down ? (
              <Chip tone="warn" dot>Server not running</Chip>
            ) : (
              <>
                <div className="segmented" role="radiogroup" aria-label="Mode" style={{ width: 260 }}>
                  {(['off', 'copilot', 'auto'] as Mode[]).map((m) => (
                    <button key={m} role="radio" aria-checked={m === mode} aria-pressed={m === mode} onClick={() => void setMode(m)} title={MODE_INFO[m].help}>
                      {MODE_INFO[m].label}
                    </button>
                  ))}
                </div>
                <span className="hint">{MODE_INFO[mode].help}</span>
                {cur && <Chip tone="danger"><span className="dot live-dot" />On a call with {cur.caller}</Chip>}
              </>
            )}
          </header>
          <main className="content">
            {down && route.page !== 'settings' ? (
              <ErrorCard message="server unreachable" onRetry={() => location.reload()} />
            ) : (
              <>
                {route.page === 'home' && <HomePage state={state} />}
                {route.page === 'calls' && <CallsPage arg={route.arg} query={route.query} />}
                {route.page === 'ask' && <AskPage key={route.query.toString()} query={route.query} chat={route.query.get('chat') || undefined} />}
                {route.page === 'brain' && (route.arg ? <BrainDetailPage chat={route.arg} /> : <BrainPage />)}
                {route.page === 'contacts' && <ContactsPage />}
                {route.page === 'settings' && <SettingsPage />}
              </>
            )}
          </main>
        </div>
      </div>
      <Palette open={palette} onClose={() => setPalette(false)} />
      <Toast text={toast} onDone={() => setToast(null)} />
    </ToastCtx.Provider>
  );
}

createRoot(document.getElementById('root')!).render(<Dashboard />);
