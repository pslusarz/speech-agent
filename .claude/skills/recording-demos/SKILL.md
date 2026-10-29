---
name: recording-demos
description: Record the speech-agent demo - the README GIF of reading, a spoken question, a spoken answer and carrying on - with headless Playwright against the app's scripted demo mode. Use when re-recording or changing docs/demo.gif, or making another clip of the app.
---

# Recording demos of speech-agent

The README GIF (`docs/demo.gif`, about 30 s and 2.6 MB) shows one arc:
1. the book is read aloud, with each word lit as it is heard;
2. a question is asked with "Hold to speak";
3. the agent answers aloud, its chat line growing word by word;
4. "carry on" resumes the reading at the sentence where the question was asked.

This adapts the `recording-demo-clips` skill from the user's
[articles](https://github.com/pslusarz/articles) repo.

## The run is scripted, not live

- **`DEMO=1` turns on demo mode.** The app answers from `REPLIES` in `src/main/demo.py`,
  in order, instead of calling Bedrock. That way every take looks the same, costs
  nothing, and needs no AWS login.
- **The rest is real:** Kokoro reads and speaks, Parakeet transcribes, and the player,
  the voice and cutting in all run as usual.
- **Run the demo copy on its own port and log, never the user's server:**

  ```bash
  DEMO=1 LOG_FILE=logs/demo.log uv run uvicorn main.app:app --port 8001
  ```

- **The scripted replies are used up after one take.** Restart the demo server before
  every take.

## Record and cut

```bash
uv run --with playwright --with imageio-ffmpeg python .claude/skills/recording-demos/record_demo.py
uv run --with imageio-ffmpeg python .claude/skills/recording-demos/record_demo.py --cut-only   # re-cut the last take
```

- **Tools:** both come from PyPI.
  - Run `uv run --with playwright python -m playwright install chromium` once.
  - ffmpeg is bundled in `imageio-ffmpeg`, so none is needed on the system.
- **The raw take** is saved as `logs/demo-raw.webm`. Recording takes about 45 s.
- **Check the contact sheet,** `logs/demo-sheet.png`, before using the GIF. It shows in
  one image whether the arc reads. Neither working file belongs in the repo.

## How the recorder drives the page

- **The microphone is replaced.** An init script overrides `getUserMedia` with a stream
  of a question spoken by Kokoro (`am_michael`), so no one's voice is recorded.
  - The question only starts once the speak button has turned red (`#speak.recording`).
  - The reason: headless Chromium takes about 2.5 s to open a microphone, and anything
    said before that would be lost.
- **Captions say what is heard,** since a GIF has no sound. They are a fixed `#caption`
  element injected into the page and updated at each step.
- **Wait on the page's state, never on the clock alone.** For example, wait for
  `#text span.lit`, for the user's bubble, for `.row.agent.speaking` to appear and then
  go, and for the book's `<audio>` to play again.

## Cut settings

- 1.45x speed, 10 fps, 860 px wide.
- A two-pass palette (`palettegen=stats_mode=diff`, then `paletteuse=dither=none`).
- **Denoise before the palette (`hqdn3d=3:3:8:8`). This matters.** Playwright records
  lossy VP8, whose shimmer makes every frame look new to the GIF encoder: 6.5 MB without
  the denoise, 2.6 MB with it.
- **Do not drop duplicate frames** (`mpdecimate`). It looks much smaller, but it loses
  frame timing: 23 s instead of 30, and the word highlight jumps.
- **If the GIF is too large, trim the take before lowering the quality.**

## Bugs recording has turned up

Recording drives the app harder than a person does, so watch the demo log
(`logs/demo.log`) for:
- **`audio-seeked` with `"at": 0`.** Audio routes must answer byte ranges, or resume
  replays the whole segment.
- **`mic-empty` right after `mic-start`.** A hold must stop the microphone only once.
- **Jittery text.** The lit word should scroll into view only when it would leave it.

All three were found this way and fixed.
