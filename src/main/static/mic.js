// The microphone, as 16 kHz mono 16-bit WAV: the format the recognisers expect.
//
// The browser's echo cancellation, noise suppression and gain control are asked for,
// so the agent's own voice through the speakers is taken out of what is recorded.
// Samples are captured raw on the audio thread, resampled here and encoded as WAV,
// so the server needs no decoder.

const mic = (() => {
  const RATE = 16000;
  let stream = null, ctx = null, node = null, blocks = [];

  async function start() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true},
    });
    ctx = new AudioContext();
    await ctx.audioWorklet.addModule(typeof WORKLET === 'string' ? WORKLET : '/static/capture-worklet.js');
    node = new AudioWorkletNode(ctx, 'capture');
    blocks = [];
    node.port.onmessage = e => blocks.push(e.data);
    // a node no one listens to may not be run, so it goes to a silent output
    const mute = ctx.createGain();
    mute.gain.value = 0;
    ctx.createMediaStreamSource(stream).connect(node).connect(mute).connect(ctx.destination);
    const settings = stream.getAudioTracks()[0].getSettings();
    return {rate: ctx.sampleRate, echoCancellation: settings.echoCancellation,
            noiseSuppression: settings.noiseSuppression};
  }

  async function stop() {
    if (!ctx) return null;
    stream.getTracks().forEach(t => t.stop());
    node.port.onmessage = null;
    const from = ctx.sampleRate;
    await ctx.close();
    ctx = null;
    const n = blocks.reduce((k, b) => k + b.length, 0);
    if (!n) return null;
    const raw = new Float32Array(n);
    let at = 0;
    for (const b of blocks) { raw.set(b, at); at += b.length; }
    // the browser's own resampler, run offline
    const off = new OfflineAudioContext(1, Math.round(n * RATE / from), RATE);
    const buf = off.createBuffer(1, n, from);
    buf.copyToChannel(raw, 0);
    const src = off.createBufferSource();
    src.buffer = buf;
    src.connect(off.destination);
    src.start();
    const out = (await off.startRendering()).getChannelData(0);
    return {wav: encode(out, RATE), seconds: out.length / RATE};
  }

  function encode(samples, rate) {
    const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
    const text = (at, s) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    text(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); text(8, 'WAVE');
    text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    text(36, 'data'); view.setUint32(40, samples.length * 2, true);
    samples.forEach((s, i) => view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
    return new Blob([view], {type: 'audio/wav'});
  }

  return {start, stop, get recording() { return ctx !== null; }};
})();

// Hold a button to record, let go to finish. The pointer is captured while held, so
// drifting off the button does not end it, and a press too short to record anything
// says so instead of vanishing.
function holdToRecord(button, note, {onPress = () => {}, onDone}) {
  let starting = null;

  async function begin(e) {
    e.preventDefault();
    if (mic.recording || starting) return;
    try { button.setPointerCapture(e.pointerId); } catch (_) {}
    // whatever the press is for, a failure there must not stop the recording
    try { onPress(); } catch (err) { log('press-failed', {message: String(err), button: button.id}); }
    note.textContent = 'Starting the microphone...';
    starting = mic.start().then(info => {
      log('mic-start', {...info, button: button.id});
      button.classList.add('recording');
      note.textContent = 'Listening... let go when you are done.';
      return true;
    }).catch(err => {
      log('mic-refused', {name: err.name, message: err.message, button: button.id});
      note.textContent = 'The microphone could not be opened: ' + err.message;
      return false;
    });
  }

  async function end() {
    // letting go fires both pointerup and lostpointercapture: only the first one ends it
    const pending = starting;
    if (!pending) return;
    starting = null;
    const opened = await pending;
    button.classList.remove('recording');
    if (!opened) return;
    const got = await mic.stop();
    if (!got || got.seconds < 0.3) {
      log('mic-empty', {seconds: got ? +got.seconds.toFixed(2) : 0, button: button.id});
      note.textContent = 'Nothing was recorded - hold the button down while you speak. ' +
        '(If the browser asked to use the microphone, allow it and try again.)';
      return;
    }
    log('mic-stop', {seconds: +got.seconds.toFixed(2), bytes: got.wav.size, button: button.id});
    await onDone(got);
  }

  button.addEventListener('pointerdown', begin);
  for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(ev, end);
}

// press to speak: pressing cuts in at once, letting go sends what was said
(() => {
  const button = document.getElementById('speak');
  if (!button) return;
  const note = document.getElementById('speak-note');
  holdToRecord(button, note, {
    onPress() {
      // quiet at once on the page; the server stops the voice and pauses the book too
      fetch('/speech/start', {method: 'POST'});
      hush();
    },
    async onDone(got) {
      const form = new FormData();
      form.append('audio', got.wav, 'speech.wav');
      note.textContent = 'Transcribing...';
      const res = await fetch('/speech', {method: 'POST', body: form});
      if (!res.ok) { note.textContent = 'Not understood: ' + await res.text(); return; }
      const heard = await res.json();
      log('speech-heard', heard);
      note.textContent = heard.text ? '' : 'No words were heard. Try again, a little closer to the microphone?';
    },
  });
})();
