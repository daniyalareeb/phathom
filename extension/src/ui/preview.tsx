// preview.tsx — dev-only mock-data preview (no server, no call needed).
// Built to preview.html; used for screenshots and visual checks.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Card, SectionTitle, Chip, Empty, SkeletonList, ErrorCard, btnPrimary, btnGhost } from './lib';
import './app.css';

const calls = [
  { caller: 'Ahmed Uni', mode: 'assistant', dur: '4 min', ago: '2 h ago', gist: 'Report draft will be sent tonight.' },
  { caller: 'Ammi', mode: 'copilot', dur: '12 min', ago: 'yesterday', gist: 'Grocery list and Sunday plan.' },
];

function Preview() {
  const [theme, setTheme] = React.useState<'light' | 'dark'>(
    (new URLSearchParams(location.search).get('theme') as 'light' | 'dark') || 'light',
  );
  React.useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);
  const page = new URLSearchParams(location.search).get('page') || 'home';
  return (
    <div style={{ padding: 18, maxWidth: 760, display: 'grid', gap: 12 }}>
      <div style={{ display: 'flex', gap: 8 }}>
        <Chip tone="live">● Connected</Chip>
        <span style={{ fontSize: 13, color: 'var(--fg-2)' }}>Answer: Talk · Copilot: On</span>
        <span style={{ flex: 1 }} />
        <button onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} style={btnGhost}>
          {theme}
        </button>
      </div>
      {page === 'home' && (
        <>
          <Card><SectionTitle>Live call</SectionTitle><Empty text="No live call." hint="Call yourself to test Copilot." /></Card>
          <Card>
            <SectionTitle>Last calls (mock)</SectionTitle>
            {calls.map((c) => (
              <div key={c.caller} style={{ padding: '8px 0', borderBottom: '1px solid var(--hairline)', fontSize: 14 }}>
                <strong>{c.caller}</strong> <Chip>{c.mode}</Chip>{' '}
                <span style={{ fontSize: 12, color: 'var(--fg-2)' }}>{c.dur} · {c.ago}</span>
                <div style={{ fontSize: 13, color: 'var(--fg-2)' }}>{c.gist}</div>
              </div>
            ))}
          </Card>
          <Card><SectionTitle>Loading state</SectionTitle><SkeletonList rows={2} /></Card>
          <Card><SectionTitle>Side panel transcript (mock)</SectionTitle>
            <div style={{ fontSize: 13 }}><strong style={{ color: 'var(--accent)' }}>You · </strong>hello, can you hear me?</div>
            <div style={{ fontSize: 13 }}><strong>Caller · </strong>yes, loud and clear.</div>
          </Card>
        </>
      )}
      {page === 'error' && <ErrorCard message="Server isn't running — start it with phathom serve." onRetry={() => {}} />}
      {page === 'call' && (
        <Card>
          <h2 style={{ margin: '0 0 6px' }}>Report draft discussion</h2>
          <div style={{ background: 'var(--accent-soft)', borderRadius: 8, padding: 10, fontSize: 14 }}>
            <strong>Message for you: </strong>Ahmed will send the report draft tonight.
          </div>
          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
            <button style={btnPrimary}>Play mix</button>
            <button style={btnGhost}>Reprocess</button>
          </div>
        </Card>
      )}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Preview />);
