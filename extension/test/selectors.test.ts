import { describe, expect, it } from 'vitest';
import { SEL, OUTGOING_PEER_CANDIDATES } from '../src/content/selectors';
// Keep the TS selectors in sync with phathom/whatsapp/selectors.py (verified set).
import * as fs from 'fs';
import * as path from 'path';

describe('selectors mirror selectors.py', () => {
  it('verified testids match the Python source', () => {
    const py = fs.readFileSync(path.resolve(__dirname, '../../phathom/whatsapp/selectors.py'), 'utf8');
    for (const testid of ['chat-list', 'voip-call-participant-info-name', 'voip-call-timer']) {
      expect(py).toContain(testid);
    }
    expect(SEL.LOGGED_IN).toContain('chat-list');
    expect(SEL.INCOMING_CALLER).toContain('voip-call-participant-info-name');
    expect(SEL.CALL_TIMER).toContain('voip-call-timer');
  });

  it('outgoing peer + chat selectors stay flagged UNVERIFIED', () => {
    // If any of these ever matches a dump, move it to SEL and drop the flag.
    expect(OUTGOING_PEER_CANDIDATES.length).toBeGreaterThan(0);
  });
});
