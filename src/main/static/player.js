// Plays what the server's player prepares, and tells it where playback is.
//
// The server decides what is read; the page only plays it. Each event carries the
// session it belongs to, and anything from a session the page has moved on from is
// ignored, so audio made for an old reading never plays.

const REPORT_MS = 1000;
const audio = document.getElementById('audio');
let session = 0, chapter = null, segs = [], cur = -1, wanted = null, last = false;
let starts = [], spans = [], lit = null;

// everything worth knowing goes to the server log, batched so it does not flood it
const LOG_FLUSH_MS = 500;
let pending = [];
function log(kind, data) {
  pending.push({kind, data: {...data, at: +audio.currentTime.toFixed(2), cur, session, paused: audio.paused}});
}
setInterval(() => {
  if (!pending.length) return;
  const batch = pending; pending = [];
  fetch('/log', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({entries: batch})});
}, LOG_FLUSH_MS);
window.addEventListener('error', e => log('js-error', {message: e.message, line: e.lineno}));
window.addEventListener('unhandledrejection', e => log('js-rejection', {reason: String(e.reason)}));
for (const ev of ['play', 'playing', 'pause', 'ended', 'waiting', 'stalled', 'error', 'loadedmetadata', 'seeked'])
  audio.addEventListener(ev, () => log('audio-' + ev, {src: audio.src.split('/').pop(), duration: audio.duration,
    error: audio.error && audio.error.message}));

const post = (url, data) => fetch(url, {
  method: 'POST',
  headers: {'Content-Type': 'application/x-www-form-urlencoded'},
  body: new URLSearchParams(data),
});

function report() {
  if (cur >= 0) post('/player/report', {session, index: cur, t: audio.currentTime});
}

async function showChapter(id) {
  if (id === chapter || id === null) return;
  chapter = id;
  const html = await (await fetch('/chapter/' + id)).text();
  document.getElementById('text').innerHTML = html;
  spans = [...document.querySelectorAll('#text span[data-s]')];
  starts = spans.map(s => +s.dataset.s);
  lit = null;
}

// play segment i from t seconds, or wait for it if it has not been made yet
function start(i, t) {
  log('start', {index: i, t, made: !!segs[i]});
  if (!segs[i]) { wanted = {index: i, t}; return; }
  wanted = null;
  cur = i;
  audio.src = '/audio/' + segs[i].id;
  audio.addEventListener('loadedmetadata', () => {
    audio.currentTime = t;
    audio.play().catch(err => {
      log('play-refused', {name: err.name, message: err.message});
      setStatus('Press resume to start the audio.');
    });
  }, {once: true});
}

function setStatus(text) { document.getElementById('player-note').textContent = text; }

audio.addEventListener('ended', () => {
  report();
  if (segs[cur + 1]) start(cur + 1, 0);
  else if (!last) { wanted = {index: cur + 1, t: 0}; setStatus('Preparing more audio...'); }
});
audio.addEventListener('playing', () => setStatus(''));
setInterval(() => { if (!audio.paused) report(); }, REPORT_MS);

// the word being heard: the last one in this segment that has begun
function highlight() {
  requestAnimationFrame(highlight);
  const seg = segs[cur];
  if (!seg || !starts.length) return;
  const t = audio.currentTime;
  let w = null;
  for (const x of seg.words) { if (x[2] <= t) w = x; else break; }
  if (!w) return;
  let lo = 0, hi = starts.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (starts[mid] <= w[0]) lo = mid; else hi = mid - 1; }
  const span = spans[lo];
  if (span === lit) return;
  lit?.classList.remove('lit');
  span.classList.add('lit');
  span.scrollIntoView({block: 'center', behavior: 'smooth'});
  lit = span;
}
requestAnimationFrame(highlight);

// the agent's voice: its parts play in turn, and the server is told where, often,
// since that is what decides how much of a reply the person heard when they cut in
const VOICE_REPORT_MS = 250;
const voiceEl = document.getElementById('voice');
let vq = [], vcur = null;
// the agent's line in the chat grows with its voice, so nothing unspoken is shown
const spoken = {};
function lineFor(call) {
  if (!spoken[call]) {
    const el = document.createElement('div');
    el.className = 'row agent speaking';
    el.innerHTML = '<span class="bubble"></span>';
    document.getElementById('transcript').append(el);
    spoken[call] = {el, done: ''};
  }
  return spoken[call];
}
function showSpoken() {
  requestAnimationFrame(showSpoken);
  if (!vcur) return;
  const line = lineFor(vcur.call), t = voiceEl.currentTime;
  let end = 0;
  for (const w of vcur.words) { if (w[2] <= t) end = w[1]; else break; }
  const text = line.done + vcur.text.slice(0, end);
  if (line.el.firstChild.textContent !== text) line.el.firstChild.textContent = text;
}
requestAnimationFrame(showSpoken);

function vplay() {
  if (vcur || !vq.length) return;
  vcur = vq.shift();
  lineFor(vcur.call);
  voiceEl.src = '/voice/' + vcur.id;
  voiceEl.play().catch(err => {
    log('voice-play-refused', {id: vcur.id, name: err.name, message: err.message});
    setStatus('Press anywhere on the page to let the agent speak.');
  });
}
function vreport(ended) {
  if (vcur) post('/voice/report', {id: vcur.id, t: voiceEl.currentTime, playing: voiceEl.paused ? 0 : 1, ended: ended ? 1 : 0});
}
voiceEl.addEventListener('playing', () => vreport(false));
voiceEl.addEventListener('ended', () => {
  vreport(true);
  const line = lineFor(vcur.call);
  line.done += vcur.text;
  line.el.firstChild.textContent = line.done;
  vcur = null;
  vplay();
});
for (const ev of ['play', 'pause', 'ended', 'waiting', 'error'])
  voiceEl.addEventListener(ev, () => log('voice-' + ev, {id: vcur && vcur.id, at: +voiceEl.currentTime.toFixed(2)}));
setInterval(() => { if (vcur && !voiceEl.paused) vreport(false); }, VOICE_REPORT_MS);

const handlers = {
  sync(e) {
    session = e.session; segs = []; last = e.last;
    e.segments.forEach(s => segs[s.index] = s);
    showChapter(e.chapter);
    if (e.state === 'playing') start(e.index, e.t);
    else if (segs[e.index]) { cur = e.index; audio.src = '/audio/' + segs[cur].id; }
  },
  session(e) {
    audio.pause(); session = e.session; segs = []; cur = -1; last = false;
    wanted = {index: 0, t: 0};
    setStatus('Preparing audio...');
    showChapter(e.chapter);
  },
  segment(e) {
    segs[e.index] = e; last = e.last;
    if (wanted && wanted.index === e.index) start(wanted.index, wanted.t);
  },
  pause() { wanted = null; audio.pause(); report(); },
  play(e) { start(e.index, e.t); },
  finished() { setStatus('Finished the chapter.'); },
  'voice-part'(e) { vq.push(e); vplay(); },
  'voice-stop'() {
    voiceEl.pause(); vq = []; vcur = null;
    for (const l of Object.values(spoken)) l.el.classList.remove('speaking');
  },
  'voice-done'() { for (const l of Object.values(spoken)) l.el.classList.remove('speaking'); },
  error(e) { setStatus('The player failed: ' + e.message); },
};

const es = new EventSource('/player/events');
es.addEventListener('open', () => log('stream-open', {}));
es.addEventListener('error', () => log('stream-error', {state: es.readyState}));
es.addEventListener('player', m => {
  const e = JSON.parse(m.data);
  const {words, segments, ...brief} = e;
  // the voice is not tied to a reading, so its events are not filtered by session
  const own = /^(voice-|held|released)/.test(e.kind);
  if (!own && e.kind !== 'sync' && e.session !== session && e.kind !== 'session') {
    log('server-event-ignored', brief);
    return;
  }
  log('server-event', {...brief, segments: segments && segments.length});
  handlers[e.kind]?.(e);
});

document.getElementById('text').addEventListener('click', ev => {
  const s = ev.target.closest('span[data-s]');
  if (s && chapter !== null) post('/player/play', {chapter, offset: s.dataset.s});
});
