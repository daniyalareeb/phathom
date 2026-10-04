// Shared UI primitives (light theme only): server state, cards, chips, empty /
// error states, speaker helpers and formatting.
import React from 'react';
import { api } from '../shared/api';

export function useServerState(pollMs = 5000) {
  const [state, setState] = React.useState<Record<string, unknown> | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const load = React.useCallback(async () => {
    try {
      const s = await api.state();
      setState(s);
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);
  React.useEffect(() => {
    void load();
    const id = setInterval(() => void load(), pollMs);
    return () => clearInterval(id);
  }, [load, pollMs]);
  return { state, error, reload: load };
}

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Card({ children, style, tight, className = '' }: { children: React.ReactNode; style?: React.CSSProperties; tight?: boolean; className?: string }) {
  return (
    <div className={`card ${tight ? 'card-tight' : ''} ${className}`} style={style}>
      {children}
    </div>
  );
}

export function SectionTitle({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  if (!action) return <h3 className="eyebrow">{children}</h3>;
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
      <h3 className="eyebrow">{children}</h3>
      {action}
    </div>
  );
}

export function SkeletonList({ rows = 3, height = 52 }: { rows?: number; height?: number }) {
  return (
    <div aria-label="Loading" style={{ display: 'grid', gap: 8 }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height }} />
      ))}
    </div>
  );
}

export function Empty({ text, hint, action }: { text: string; hint?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div style={{ textAlign: 'center', padding: '28px 12px' }}>
      <div style={{ fontWeight: 600 }}>{text}</div>
      {hint && <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>{hint}</div>}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}

export function ErrorCard({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const down = /unreachable/.test(message);
  return (
    <div role="alert" className="card card-tight" style={{ borderLeft: '3px solid var(--warn)' }}>
      <div style={{ fontWeight: 600 }}>{down ? 'Can’t reach the Phathom server' : 'Something went wrong'}</div>
      <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
        {down ? (
          <>
            Start it in a terminal with <code>.venv/bin/phathom serve</code>, then retry.
          </>
        ) : (
          message
        )}
      </div>
      {onRetry && (
        <button onClick={onRetry} className="btn btn-sm" style={{ marginTop: 10 }}>
          Retry
        </button>
      )}
    </div>
  );
}

type Tone = 'neutral' | 'live' | 'warn' | 'danger' | 'accent' | 'caller';
export function Chip({ children, tone = 'neutral', dot }: { children: React.ReactNode; tone?: Tone; dot?: boolean }) {
  const cls = { neutral: '', live: 'chip-live', warn: 'chip-warn', danger: 'chip-rec', accent: 'chip-accent', caller: 'chip-caller' }[tone];
  return (
    <span className={`chip ${cls}`}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

export function Toast({ text, onDone }: { text: string | null; onDone: () => void }) {
  React.useEffect(() => {
    if (!text) return;
    const id = setTimeout(onDone, 2400);
    return () => clearTimeout(id);
  }, [text, onDone]);
  return text ? <div className="toast" role="status">{text}</div> : null;
}

// ---- speakers + modes ----

export const speakerName = (s: string) => (s === 'owner' ? 'You' : s === 'phathom' ? 'Phathom' : 'Caller');
export const speakerClass = (s: string) => (s === 'owner' ? 'sp-owner' : s === 'phathom' ? 'sp-phathom' : 'sp-caller');

/** DB call.mode / live session kind → the words the UI uses. */
export const modeLabel = (m?: string | null) =>
  ({ notes: 'Listen', listen: 'Listen', assistant: 'Talk', talk: 'Talk', copilot: 'Copilot', manual: 'Manual' } as Record<string, string>)[m || ''] || m || '—';

export function ModeChip({ mode }: { mode?: string | null }) {
  const label = modeLabel(mode);
  return <Chip tone={label === 'Talk' ? 'accent' : label === 'Copilot' ? 'live' : label === 'Listen' ? 'caller' : 'neutral'}>{label}</Chip>;
}

/** Talk share: how much of the call each side spoke, from transcript segments. */
export function TalkShare({ segments, showLegend = true }: { segments: { t_start: number; t_end: number; speaker: string }[]; showLegend?: boolean }) {
  const tot: Record<string, number> = { caller: 0, owner: 0, phathom: 0 };
  for (const s of segments) tot[s.speaker in tot ? s.speaker : 'caller'] += Math.max(0, s.t_end - s.t_start);
  const sum = tot.caller + tot.owner + tot.phathom;
  if (!sum) return null;
  const parts = (['caller', 'owner', 'phathom'] as const).filter((k) => tot[k] > 0);
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div className="talkshare" role="img" aria-label={parts.map((k) => `${speakerName(k)} ${Math.round((tot[k] / sum) * 100)}%`).join(', ')}>
        {parts.map((k) => (
          <span key={k} className={speakerClass(k)} style={{ flex: tot[k] }} />
        ))}
      </div>
      {showLegend && (
        <div className="legend">
          {parts.map((k) => (
            <span key={k} className={speakerClass(k)}>
              <i />
              {speakerName(k)} <span className="mono">{Math.round((tot[k] / sum) * 100)}%</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- the three modes (one control instead of Connect + Answer + Copilot) ----

export type Mode = 'off' | 'copilot' | 'auto';

export function modeOf(state: Record<string, unknown> | null): Mode {
  if (!state?.connected) return 'off';
  return String(state.answer ?? 'off') !== 'off' ? 'auto' : 'copilot';
}

export const MODE_PATCH: Record<Mode, Record<string, unknown>> = {
  off: { connected: false },
  copilot: { connected: true, mode: 'off', copilot: true },
  auto: { connected: true, mode: 'talk', copilot: true },
};

export const MODE_INFO: Record<Mode, { label: string; help: string }> = {
  off: { label: 'Off', help: 'Phathom does nothing. Calls ring as usual.' },
  copilot: { label: 'Copilot', help: 'Takes notes on calls you answer or make. Never picks up for you.' },
  auto: { label: 'Auto', help: 'Answers calls you miss and talks for you, and takes notes on calls you handle yourself.' },
};

export type Rule = 'allow' | 'block' | 'default';
export function ruleFor(name: string, c: { allow: string[]; block: string[] } | null): Rule {
  if (!c) return 'default';
  const n = name.trim().toLowerCase();
  if (c.block.some((x) => x.trim().toLowerCase() === n)) return 'block';
  if (c.allow.some((x) => x.trim().toLowerCase() === n)) return 'allow';
  return 'default';
}
export const RULE_LABEL: Record<Rule, string> = { allow: 'Allowed', block: 'Blocked', default: 'Default rule' };

// ---- formatting ----

export const initial = (name?: string | null) => (name || '?').trim().charAt(0).toUpperCase() || '?';

export function fmtAgo(iso?: string | null): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!t) return '—';
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

export function fmtDur(s?: number | null): string {
  if (s == null) return '—';
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`;
}

export const fmtClock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

export function fmtTime(iso?: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function dayLabel(iso?: string | null): string {
  if (!iso) return 'Unknown date';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return 'Unknown date';
  const today = new Date();
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'short' });
}

// Legacy style objects, still used by the dev-only preview page.
export const btnPrimary: React.CSSProperties = { background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 10, padding: '9px 14px', fontWeight: 600, cursor: 'pointer' };
export const btnGhost: React.CSSProperties = { background: 'var(--surface)', color: 'var(--ink)', border: '1px solid var(--line-strong)', borderRadius: 10, padding: '8px 14px', fontWeight: 600, cursor: 'pointer' };
export const btnDanger: React.CSSProperties = { ...btnPrimary, background: 'var(--rec)' };
