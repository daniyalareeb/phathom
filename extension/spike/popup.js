// popup.js — spike triggers: test tone + DOM dump.
const statusEl = document.getElementById('status');
function setStatus(s) { try { statusEl.textContent = s; } catch (e) {} }
async function bgStatus() {
  try {
    const r = await chrome.runtime.sendMessage({ cmd: 'bg_status' });
    setStatus('bg: ' + JSON.stringify(r));
  } catch (e) { setStatus('bg unreachable: ' + e); }
}
document.getElementById('tone').addEventListener('click', async () => {
  setStatus('sending play_tone…');
  try {
    const r = await chrome.runtime.sendMessage({ cmd: 'play_tone' });
    setStatus('play_tone: ' + JSON.stringify(r) + '\nAsk the caller if they heard a 3 s beep.');
  } catch (e) { setStatus('play_tone failed: ' + e); }
});
document.getElementById('dump').addEventListener('click', async () => {
  setStatus('dumping DOM…');
  try {
    const r = await chrome.runtime.sendMessage({ cmd: 'dump_dom', label: 'manual' });
    setStatus('dump: ' + JSON.stringify(r));
  } catch (e) { setStatus('dump failed: ' + e); }
});
bgStatus();
