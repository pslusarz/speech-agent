---
name: interrupts
description: How speech-agent stops, truncates and coordinates when the person cuts in - the streaming model call, the agent's voice, the book player and what goes into history. Use before changing the harness's model call, voice.py, the player's hold/release, cut_in(), or when adding spoken input that triggers a barge-in.
---

# Interruption in speech-agent

Everything here exists to keep one rule: **what the person did not hear was never
said.** The history, the chat and the agent's own sense of where it stopped all follow
the audio the person actually heard.

## The pieces

**1. The streaming model call** is `Agent._call` in `harness/agent.py`.
- It iterates over **every** stream event and checks the `_interrupting` flag on each one.
- `Agent.interrupt()` sets that flag, then waits until the turn has stopped.
- **Never call `stream.close()` from another thread.** On Bedrock, `close()` returns at once but leaves the reading thread blocked for good. This was found live, and `scripts/cut_stream.py` reproduces it.
- With the flag, the reader leaves the stream at its next event (1 to 240 ms measured) and closes it in its own thread.
- Check all event types, not only text deltas, so a tool call being streamed can be cut too.

**2. What an interrupted call keeps.** `_cut` stores only `heard(call, written)`.
- That is what the voice says the person heard.
- A half-streamed tool call is dropped, because its input is incomplete and nothing ran for it.
- If any written text went unheard, a note is added telling the agent what it lost.
- Your next message is stored after that, so ordering stays valid.

**3. Replies already stored but not yet heard.** This is the common case: the model
writes about 13 times faster than the voice speaks.
- `cut_in()` in `app.py` calls `voice.stop()`, which returns `{call: (heard, unheard)}`.
- `agent.truncate(call, heard)` then edits each stored reply down to what was heard. Tool calls stay, since they ran.
- An emptied entry has `content == []`, and `render()` skips it.
- One note tells the agent where it was cut off and what it had been about to say.

**4. The voice** is `player/voice.py`.
- The `Chunker` sends the first clause or sentence past `VOICE_FIRST_CHARS` (40, about 10 tokens), then whole sentences. It cuts at a clause past 120 characters, or at a space at 300.
- Each chunk becomes a `Part`: Kokoro audio with word timings.
- The page plays the parts in order and reports `/voice/report` every 250 ms and on `ended`.
- `heard()` uses those reports, carried forward by the clock, to work out the words heard.
- `stop()` freezes every playing part at that instant (`freeze`, `cut`), so time passing afterwards changes nothing.
- **`stop()` reports only replies still being spoken.** It once reported every reply it had ever stopped. Every cut-in note then described the first interruption, and the agent kept repeating that content.

**5. One voice at a time.**
- Speaking pauses the book: `Voice.pump` calls `player.pause(by="agent")`.
- A play or resume the agent asks for is **held** while `player.hold()` is true.
- `hold` is `voice.busy or agent.busy`. It must include the turn, not just the voice. Otherwise the held play runs in the gap between two model calls of one turn, and the next call's "Enjoy the reading!" pauses the book it just started.
- `player.release()` runs from the voice's `voice-done` event and from `agent.on_idle`, and acts only once nothing holds it.
- A cut-in calls `release(cancel=True)`, which drops the held action.
- **`player_play` and `player_resume` end the turn** (`ENDS_TURN` in `player/tools.py`,
  passed to the Agent as `ends_turn`). Without this, the harness made one more model
  call after the tool result, with nothing left to say. Once the model filled that call
  by writing the person's next message ("Human: who does he meet first…"), and the voice
  spoke it.
- **The player notice describes a held action as what is about to happen** ("about to
  resume", "about to start chapter N"), never as "paused". A tool result saying
  "playing again" next to a notice saying "paused" is a contradiction, and those invite
  strange replies.

**6. The page** is `static/player.js`.
- The agent's chat line is drawn from its voice, word by word, as each word plays. Nothing written but unspoken is shown.
- The person's lines come over server-sent events as they are stored.
- On reload, the transcript is `agent.transcript()`, so cut-off replies appear cut off.

**7. Where the book resumes.**
- `player.heard_message()` marks the offset being heard when the person spoke.
- `resume()` with no offset goes back to the start of that sentence, however long the agent took to pause.

## Known gaps

- **What the page shows and what is stored can differ by a word.** The page follows its audio clock; the stored text is worked out from reports sent every 250 ms. The fix is to have the page send its exact position with the cut-in.
- **The agent can still say a line after starting the book.** The prompt asks it not to, and the hold keeps the book from being paused by it.

## Testing an interrupt

- **Mid-stream cut:** `hold = script.hold(request=n, after=k)`, post a message, `hold.reached.wait()`, then `cut(agent, hold)`. `cut()` interrupts, waits until the flag is set, then releases the stream, as the next event of a real model would arrive.
- **Voice cut:** `speak(v, call, text)` from `test_voice.py`, then `v.report(...)` to place the page, move `clock.now`, and `v.stop()`.
- **Whole cut-in:** `test_app.py` drives `/say` while the voice is busy, then checks the stored history, `agent.transcript()`, the page HTML and the note's position in the next request.

## Adding spoken barge-in

Call `cut_in()` when a spoken interruption is **confirmed**, then
`player.heard_message()` and `agent.post(transcript)`, as `/say` does.

On the first sign of speech, **pause** the voice and the book on the page without
calling `cut_in()`. Pausing is cheap and cancelling is not. If the speech turns out to
be a cough or an "uh-huh", un-pause.

Mind the rule above: `heard()` must reflect the moment the person **started**
speaking, not the moment the interruption was confirmed. Freeze the voice position at
the start of speech, or the agent will be credited with words the person talked over.
