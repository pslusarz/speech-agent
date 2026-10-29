---
name: run-and-debug
description: Run the speech-agent app, its tests and live tests, and debug a session the user has just driven by reading the shared log. Use when starting, restarting or troubleshooting the app, or when the user says "check the log".
---

# Running and debugging speech-agent

The user drives the app in their own browser; you read what happened from the log.
The browser pane cannot reach localhost here, so do not plan on driving the page
yourself.

## Run

```bash
uv sync
LLM_CACHE=off uv run uvicorn main.app:app --port 8000     # http://127.0.0.1:8000
```

- Run it in the background. It is ready when the log says `speech.app INFO ready`, about 10 to 15 s after starting.
- Startup loads the book and Kokoro. The first run ever downloads about 330 MB of Kokoro weights.
- Claude runs on Bedrock with the `development` AWS profile. Credentials are read at
  startup, so an expired SSO session makes the server exit at once, ending its output
  with `botocore.exceptions.UnauthorizedSSOTokenError: The SSO session associated with
  this profile has expired`. It expires overnight.
  - **Do not try to log in yourself.** The browser sign-in never reaches the user from
    your shell. Ask them to run `aws sso login --profile development`, wait for them to
    say it is done, then start the server.
  - **Do not work around it** by making the server start without credentials. The user
    decided against that.
  - To check before starting: `aws configure export-credentials --profile development
    >/dev/null && echo ok`.
  - The user's profiles share one SSO sign-in, so logging in to any of them (staging,
    say) refreshes `development` too. Run the check rather than asking which one they
    used.

**Never stop or restart the server without asking the user.** They may be in the
middle of testing, and a server that disappears looks to them like the app locked up.
That has happened once. If a fix needs a restart, say so and let them choose when.

## Tests

```bash
uv run pytest                                        # fast: a scripted model, silent audio, ~1 s
LLM_CACHE=off uv run pytest -m live -s src/test/test_live.py   # real Kokoro and Bedrock
```

**The fast tests** use these doubles from `src/test/conftest.py` and the tests:

| Double | What it does |
|---|---|
| `Script` | A model that says what the test wrote. It records every request and checks its shape the way the API would. |
| `Streamed` | Fakes `messages.stream()`, sending the text 4 characters per event. |
| `Hold` + `cut()` | Stop a stream partway and interrupt it there. |
| `Silent` | A TTS stand-in: 0.06 s per character, with word timings. |
| `Clock` | A clock the test moves by hand. |

Construct `Player` and `Voice` with `background=False` and drive them with `fill()` and `pump()`.

**The live tests** print their measurements:
- Kokoro word timings.
- The read, question, pause, resume sequence.
- A Bedrock stream cut off from another thread (it should stop in under 250 ms).
- Voice latency (about 1.1 to 1.3 s from the first token to the first audio).

**Avoid hangs** by running live tests under a time cap. For example, start them in
the background and kill them after 120 s. A hung live test otherwise blocks forever.

## The log: `logs/speech-agent.log`

Everything goes to one file, each line tagged with its source:

| tag | what |
|---|---|
| `speech.app` | the person's messages and button presses, with the state of the player, the voice and the agent at that moment |
| `speech.agent` | every model request (the new messages only), every response, stored entry and queued event |
| `speech.harness` | interruptions (`interrupted call N`), truncations (`cut stored reply`), and event-loop failures with their tracebacks |
| `speech.player` | every player event (session, segment, pause, play, held, released, voice-*), segment generation time against audio length, and position reports at DEBUG |
| `speech.voice` | voice parts queued, made, heard to the end, and stopped (with heard/unheard lengths per call) |
| `speech.page` | from the browser: audio-element events, server events received or ignored, refused autoplay, JS errors |

**Read a session from the last startup:**

```bash
n=$(grep -n "speech.app    INFO  ready" logs/speech-agent.log | tail -1 | cut -d: -f1)
tail -n +$n logs/speech-agent.log | grep -E "speech.app|queued part|voice-stop|held|carrying|cut stored|interrupted|stored #" | cut -c1-230
```

**Other places to look:**
- **Uvicorn's own output** (the background task's output file) shows every HTTP request and its status code. A page feature that silently does nothing is often a 404 or 400 there.
- **"what the model would be sent now"** under the chat shows the agent's exact context at that moment.

## Snags already hit, and their fixes

**fasthtml's static-file route catches any path ending in a static extension**
(`.js`, `.wav` and so on), ahead of every route, and serves it from `static_path`.
- `/static/player.js` returned 404 until `fast_app(static_path=src/main)`.
- Audio is served at `/audio/{id}` and `/voice/{id}`, with no extension.

**fasthtml refuses a JSON body that is not an object** ("JSON body must be an object",
400) before the route runs.
- The page's logger now sends `{"entries": [...]}`.
- Until that fix, no `speech.page` lines reached the log at all.

**Several open pages make a "choir".** Every open tab or window used to play the same
audio, slightly out of step, and every one reported its position.
- Now the page opened most recently plays (`player.claim`, the `active` event).
- The others go quiet with a "Play it here instead" link.
- Reports from a quiet page are ignored.
- Page log lines start with the page's id, so several pages show up in the log as
  several ids.

**Browser caching:** scripts are loaded as `/static/x.js?v=<mtime>` (`asset()` in
`app.py`). A fresh `mic.js` once ran beside a cached old `player.js`
(`ReferenceError: hush is not defined` in the page log). Add any new script through
`asset()`.

**Audio routes must answer byte ranges** (`wav_response` in `app.py`). Without
`Accept-Ranges` and `206` replies, Chrome cannot seek into a file it has not fully
buffered: `currentTime = t` silently lands at 0, and resume replays the whole segment
instead of starting at the sentence. The page log shows it as `audio-seeked` with
`"at": 0`.

**Releasing a hold fires both `pointerup` and `lostpointercapture`.** `holdToRecord` in
`mic.js` takes the pending start before awaiting it, so only the first one stops the
microphone. The second used to close it again, and that recording came out empty.

**The microphone takes 0.4 to 0.8 s to open** after a press (2.5 s in headless
Chromium). Words said in that gap are lost. Keeping it open between presses would fix
that, but the browser would show "microphone in use" all the time. The user has not
decided.

**Recording a demo** has its own skill, `recording-demos`.

**Do not judge an agent's turn as finished by its first reply.** Text written before
a tool call counts as a reply, while the turn goes on calling tools. In tests, wait on
the state you care about (`agent.wait_until(lambda: player.state == ...)`), not on
`wait_for`.

**Six-step limit:** `MAX_STEPS = 6` in the harness ends a turn **silently** when the
model only calls tools. Asked about a whole chapter, the agent can spend all six steps
on `book_read` (3,000 characters each) and say nothing. This is not fixed yet.

**Drafts versus the voice:** the model writes a reply in about 1.5 s that takes about
20 s to speak. So most cut-ins land on a reply that is already written, which is
handled by truncation, not by cutting the stream. See the `interrupts` skill.

**When something looks hung,** dump every thread's stack with
`faulthandler.dump_traceback_later(25, exit=True)` in a small script (see
`scripts/cut_stream.py`). That is how the cross-thread `close()` hang was found.
