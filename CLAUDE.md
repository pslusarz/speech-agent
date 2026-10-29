# speech-agent

An agent that reads a book aloud to a person and can be interrupted. The person cuts
in with a question, the agent pauses the reading, answers in its own voice, and the
reading can resume exactly where the question was asked. The reading is a **player
tool** the agent drives; the person can drive the same player from the page.

The text is Nietzsche's *Thus Spake Zarathustra*, in Thomas Common's 1909 translation,
which is in the public domain.
- **Bundled:** the Prologue and the first seven chapters are in `data/zarathustra.json`.
- **The whole book:** `data/book.json` in
  [books-zarathustra](https://github.com/pslusarz/books-zarathustra). Set `BOOK_PATH`
  to use it.

The repo is public.

## Where we are

- **Done:**
  - the player tool;
  - the agent's replies, streamed and spoken;
  - interruption, by typing or by **press-to-speak**: a "Hold to speak" button whose
    speech is transcribed by Parakeet v2 on the server's CPU (`src/main/asr.py`,
    `/speech/start`, `/speech`);
  - `scripts/asr_bench.py`, which measures recognition on a folder of recordings;
  - a scripted demo mode (`DEMO=1`), and the README GIF recorded with it (see the
    `recording-demos` skill).
- **Measured on the user's voice (M3, int8):** 4 of 4 questions transcribed word for
  word, in 0.1 to 0.2 s with 2 threads while Kokoro worked on the same cores.
- **Next: automatic voice detection**, as a second trigger into the same path. See
  "Next step" below.

Work in small steps: one capability at a time, tested, then shown to the user.

## Stack

- **Runtime:** Python 3.13 with `uv`.
- **Web:** fasthtml (htmx, server-sent events) and one plain JS file, `src/main/static/player.js`.
- **Model:** Claude Sonnet 4.6 on AWS Bedrock.
  - Client: `AnthropicBedrock`.
  - Profile `development`, region `us-east-1`.
  - The model can be overridden with `ANTHROPIC_MODEL`.
  - It is called with `messages.stream`.
- **TTS:** Kokoro-82M on CPU (`kokoro` package), about 6 times faster than real time on an M3 Pro.
  - Returns word timestamps.
  - Voice `af_heart` for both the book and the agent.
  - espeak-ng comes in as a **library** via `espeakng-loader`, not a system binary.
  - The spaCy `en_core_web_sm` model is pinned in `pyproject.toml`, so nothing downloads at runtime except Kokoro's weights from Hugging Face.
- **Harness:** copied from `../async-agent` exp10 into `src/main/harness`:
  - a flat transcript;
  - current state shown before every turn as a notice that is never stored;
  - synchronous tools.

  The background-task machinery came along with it but is unused.

## Layout

| | |
|---|---|
| `src/main/harness/agent.py` | the loop: streaming model call, interrupt, `truncate`, notices, notes |
| `src/main/harness/trace.py` | wraps the client; feeds the debugger and the log |
| `src/main/player/book.py` | chapters as text; every position is a character offset into one chapter |
| `src/main/player/player.py` | the book player: segments, buffering, position, pause, resume, held actions |
| `src/main/player/voice.py` | the agent's voice: chunking, parts, what was heard, stop |
| `src/main/player/tts.py` | Kokoro, and `Silent` for tests |
| `src/main/player/tools.py` | the agent's tools, its system prompt, the player notice |
| `src/main/app.py` | routes, `cut_in()`, wiring, logging |
| `docs/research/2026-10-28-voice-stack-research.md` | the research behind these choices |

## Decisions the user made, and why

- **No Pipecat or LiveKit, for now.**
  - Their value is a real-time audio pipeline (voice activity detection, speech-to-text, transport, barge-in).
  - The player, with audio made minutes ahead and the agent pausing, inspecting and resuming it, does not fit their model of an LLM streaming into TTS.
  - They may still be worth it for the speech-input step: judge that then.
- **Kokoro, run locally.**
  - Free, runs on CPU, gives word timestamps.
  - English only: Polish is not supported. The user also reads Polish, so a hosted TTS (for example ElevenLabs) is the route if Polish is ever needed.
- **The agent never sees the text that has been read.**
  - It sees a one-line player notice each turn.
  - It uses `player_status` (the last passage heard), `book_read` and `book_find`.
- **Five minutes of audio are made ahead, refilled once under one minute is left** (`PLAYER_AHEAD_SECONDS`, `PLAYER_REFILL_AT_SECONDS`).
- **Text interrupts came first, speech second**, so the interrupt mechanics could be got right before any audio input.
- **The agent uses the same voice as the book's reader.**
- **The agent resumes only when asked.** Asking "Shall I resume?" is fine.
  - The current focus is interruption mechanics, not the ideal resume experience. Do not spend effort polishing that.
- **Caching streamed model calls can wait.** The `LLM_CACHE` wrapper covers only `create`, so run with `LLM_CACHE=off`.
- **The user drives when testing.**
  - Instrument everything so problems can be read from the log.
  - Never stop or restart the server without asking: it ends the user's session (see the run-and-debug skill).

## Next step: automatic voice detection

Press-to-speak works; voice detection is to trigger the same thing without a button.
Text input and the button stay.

**Where it plugs in:**
- `cut_in()` in `app.py` is the integration point. It stops the voice, interrupts the stream, truncates history to what was heard, and notes the agent.
- A confirmed spoken barge-in should call it.
- Then `player.heard_message()` and `agent.post(transcript)`, exactly as `/say` does.

**Decided with the user (2026-10-29):**
- **English only for now.** Kokoro has no Polish, so the conversation is English too.
- **Recognition runs on the server, on CPU.** The target deployment is Railway, which
  has no GPUs. The candidate is Parakeet TDT 0.6B v2 as int8 ONNX through `sherpa-onnx`,
  which runs the same on the Mac and on Linux. It is not yet benchmarked.
- **Press-to-speak first, automatic voice detection second.** Both send
  `speech-start` / `speech-end` into one path.
- **The user's voice recordings stay out of the repo.** They live in `samples/`, which
  git ignores, and `scripts/asr_bench.py` reads them from there. The in-app panel that
  recorded them has been removed.

See `docs/research/2026-10-29-speech-recognition.md`.

**The first research doc also recommends:**
- **Detect speech in the browser** with Silero VAD (`@ricky0123/vad-web`).
- **Pause, then confirm:**
  - On speech start, pause the output at once.
  - Confirm the barge-in only after about 2 to 3 words of transcript, or more than 400 ms of speech.
  - Otherwise un-pause (false-interruption resume).
- **Echo:** Chromium's echo cancellation historically ignores Web Audio output.
  - We already play through `<audio>` elements, which is the documented workaround. Keep it that way.
  - Headphones avoid echo altogether.
- **Streaming speech-to-text** is not needed for press-to-speak. For automatic detection,
  `nemotron-speech-streaming-en-0.6b` (CPU int8) gives partial words every 80 to 160 ms.

The interruption design these all build on is in the `interrupts` skill.
