# speech-agent

An agent that reads a book aloud through a player tool it can pause, inspect and resume,
while the person keeps talking to it. Built on the exp10 harness from
[async-agent](https://github.com/pslusarz/async-agent): a flat transcript, plus current
state shown before every turn and never stored.

![the agent reads aloud, is asked a question by voice, answers, and carries on reading from the same sentence](docs/demo.gif)

*The demo has no sound, so the captions say what is heard. It is a scripted run: the
agent's replies are canned, while the reading, the agent's voice and the speech
recognition are real. How it is recorded: `.claude/skills/recording-demos/`.*

The person interrupts by typing or by holding a button and speaking. The agent answers
aloud, streaming, in the book's voice, and can itself be cut off mid-reply. The reading
resumes at the sentence the person was hearing when they cut in.
- **The model:** Claude, on AWS Bedrock.
- **The speech:** Kokoro reads and speaks, and NVIDIA Parakeet recognises speech. Both
  run on the server's CPU.

Context for the next agent: [CLAUDE.md](CLAUDE.md) and the skills in `.claude/skills/`.
Research behind the choices is in `docs/research/`.

    uv sync
    LLM_CACHE=off uv run uvicorn main.app:app --port 8000     # http://127.0.0.1:8000
    uv run pytest                                             # fast, no model calls
    LLM_CACHE=off uv run pytest -m live -s                    # real Kokoro and Claude

**What it needs**
- The `development` AWS profile, for Claude on Bedrock. `AWS_PROFILE` overrides it.
- Nothing else to download by hand:
  - **The book:** the Prologue and the first seven chapters are in
    `data/zarathustra.json`. That is Thomas Common's 1909 translation, in the public
    domain. For the whole book, set `BOOK_PATH` to `data/book.json` from
    [books-zarathustra](https://github.com/pslusarz/books-zarathustra).
  - **The models:** Kokoro's weights (about 330 MB) and Parakeet's (about 660 MB)
    download from Hugging Face on first run.
  - **espeak-ng** comes as a library with `espeakng-loader`, so nothing is installed
    outside the virtualenv.

## Layout

| | |
|---|---|
| `src/main/harness` | exp10, copied: `agent.py` the loop and transcript, `run.py` the background task contract, `tools.py` tail and kill, `trace.py` what the model was sent |
| `src/main/player/book.py` | chapters as text, with paragraph and sentence spans; every position is a character offset into one chapter |
| `src/main/player/tts.py` | Kokoro, and a silent engine for tests; each returns WAV and the time of every word |
| `src/main/player/player.py` | the player: segments of a few sentences, AHEAD seconds made ahead, more once fewer than REFILL_AT are left |
| `src/main/asr.py` | speech recognition: Parakeet TDT 0.6B v2, int8, through sherpa-onnx, on the CPU |
| `scripts/asr_bench.py` | how fast and how accurate recognition is on a folder of recordings |
| `src/main/demo.py` | the scripted demo mode (`DEMO=1`) the GIF above is recorded in |
| `src/main/player/voice.py` | the agent's voice: streamed text chunked for Kokoro, parts the page plays and reports, what was heard |
| `src/main/player/tools.py` | the player and book as the agent's tools, the notice it sees each turn, the note it gets when the person clicks |
| `src/main/app.py`, `static/player.js` | the page: chat on the left, the player and the chapter on the right, the word being heard lit up |

## How the agent knows where the person is

- The page plays the audio and reports its position every second; between reports the
  player assumes the clock. The word heard comes from Kokoro's word timings.
- Before each turn the agent is shown the player's state and the sentence being heard,
  never the text read so far. `player_status` gives the last passage heard;
  `book_read` and `book_find` look up anything else.
- When the person sends a message while it plays, the player marks where they were.
  `player_resume` goes back to the start of that sentence, however long the agent took
  to pause.
- Clicks on the page drive the same player and leave the agent a note, so it knows
  who paused it.

Settings: `PLAYER_AHEAD_SECONDS` (300), `PLAYER_REFILL_AT_SECONDS` (60),
`PLAYER_SEGMENT_CHARS` (400), `KOKORO_VOICE` (`af_heart`).
