# speech-agent

An agent that reads a book aloud through a player tool it can pause, inspect and resume,
while the person keeps talking to it. Built on the exp10 harness from
[async-agent](../async-agent): a flat transcript, plus current state shown before every
turn and never stored.

The person types questions; the agent answers aloud, streaming, in the book's voice,
and can be cut off mid-reply. The person's own speech comes next. Context for the next
agent: [CLAUDE.md](CLAUDE.md) and the skills in `.claude/skills/`. Research behind the choices:
[docs/research/2026-10-28-voice-stack-research.md](docs/research/2026-10-28-voice-stack-research.md).

    uv sync
    LLM_CACHE=off uv run uvicorn main.app:app --port 8000     # http://127.0.0.1:8000
    uv run pytest                                             # fast, no model calls
    LLM_CACHE=off uv run pytest -m live -s                    # real Kokoro and Claude

Needs the `development` AWS profile for Claude on Bedrock (`AWS_PROFILE` overrides it),
and the book from [books-zarathustra](https://github.com/pslusarz/books-zarathustra)
cloned beside this repo (`BOOK_PATH` overrides it). Kokoro's weights (about 330 MB)
download from Hugging Face on first run; espeak-ng comes as a library with
`espeakng-loader`, so nothing is installed outside the virtualenv.

## Layout

| | |
|---|---|
| `src/main/harness` | exp10, copied: `agent.py` the loop and transcript, `run.py` the background task contract, `tools.py` tail and kill, `trace.py` what the model was sent |
| `src/main/player/book.py` | chapters as text, with paragraph and sentence spans; every position is a character offset into one chapter |
| `src/main/player/tts.py` | Kokoro, and a silent engine for tests; each returns WAV and the time of every word |
| `src/main/player/player.py` | the player: segments of a few sentences, AHEAD seconds made ahead, more once fewer than REFILL_AT are left |
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
