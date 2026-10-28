# Voice Stack Research: Read, Interrupt, Answer, Resume

Date: 2026-10-28. GitHub star counts were read directly from the repository pages on this date.

## Scenario

An agent reads a text aloud using a `read_aloud` tool. The listener interrupts by speaking and asks a question. The agent sees that the tool is paused and where it stopped, answers by voice, and then resumes reading from the point of interruption. Speech should start after about 10 LLM tokens.

## Summary

1. **Pipecat is not the only option.** It is one of two strong open-source choices; the other is **LiveKit Agents**. Both support Claude, and both cut the conversation context back to the words the user actually heard.
2. **No product or open-source project ships "resume reading after a question".** NotebookLM Interactive mode comes closest, but it is closed and its behaviour is undocumented. The resume logic is ours to build. All the parts it needs are documented.
3. **Word-level position requires a TTS service that returns timestamps.** The strongest hosted options are Cartesia Sonic, ElevenLabs Flash v2.5 and Inworld TTS-2. Among open models, only Kyutai TTS returns word timestamps natively and also accepts text one token at a time.
4. **Running TTS on our own server is cheap.** It costs a few cents per listening-hour with Kokoro or Kyutai, compared with $1 to $4 per hour on hosted APIs. The real cost risk is paying for GPUs that sit idle.
5. **The main web-app risk is echo.** Chrome's echo cancellation has historically not filtered audio played through Web Audio. On laptop speakers, the agent can hear itself and trigger a false interruption.

---

## 1. Frameworks

| # | Framework | Stars | Last release or activity | Trims context to heard words? | Claude support | Verdict |
|---|---|---|---|---|---|---|
| 1 | **Pipecat** (BSD-2, Python) | 16.9k | v1.14.0, 2026-10-24 | Yes. Text is released in sync with playback, and only spoken text is committed to context. | Native Anthropic service | Best default |
| 2 | **LiveKit Agents** (Apache-2.0, Python and Node) | 15.0k (JS SDK: 952) | v1.8.9, 2026-10-24 | Yes. Adds per-speech `interrupt()` handles and false-interruption resume. | Plugin | Equal; best if we want WebRTC and scaling built in |
| 3 | OpenAI Agents SDK / Realtime API | 30.4k | v0.24.0, 2026-10-15 | Yes over WebRTC. Over WebSocket the client must call `conversation.item.truncate`, and the docs say the transcript is not trimmed accurately. | Realtime API: no | Poor fit (vendor lock-in) |
| 4 | TEN Framework | 11.2k | 2026-10-21 | Not documented | Through extensions (unverified) | Watch |
| 5 | Vision Agents (Stream) | 8.2k | Active | Not documented | Unclear | Watch |
| 6 | RealtimeTTS / RealtimeSTT | 4.0k / 10.2k | 2026-10-28 | We would build it (word-timing callbacks exist) | Yes (we supply the LLM) | Do-it-yourself option |
| 7 | FastRTC (Gradio) | 4.6k | Quiet since January 2026 | No | Any Python callable | Avoid |
| 8 | Kyutai Unmute | 1.5k | 2025-09 | Not documented | OpenAI-compatible LLM endpoints | Avoid |
| 9 | Vocode | 3.8k | Last commit 2024-11 | Partial | Yes | Dead |

**Hosted platforms** (Vapi, Retell, ElevenLabs Agents, Deepgram Voice Agent) are the fastest way to a demo. None documents a way to report exactly which words were played. A Vapi support thread asking how to find the interruption point went unanswered.

**Speech-to-speech models** (Gemini Live, AWS Nova Sonic, Ultravox) lock us to the vendor's own model. Forum reports describe missed barge-ins on Gemini Live, and context that includes words the user never heard.

**Other notes:**
- Pipecat Flows was archived on 2026-07-05 and now ships inside Pipecat as `pipecat.flows`.
- Smart Turn v3.2 (1.6k stars, 8M parameters, runs on CPU) is an end-of-turn detector. Several frameworks use it.

## 2. Hosted streaming TTS

| Vendor / model | Accepts text token by token | Timestamps | Time to first audio: vendor claim vs measured by Coval | Price per 1M characters | Direct from browser |
|---|---|---|---|---|---|
| **Cartesia Sonic 3.5** | Yes. `context_id` with `continue:true` keeps prosody consistent across fragments. | Per word, plus phonemes | Under 90 ms claimed / 270 ms measured | about $37–50 | Yes (short-lived token) |
| **ElevenLabs Flash v2.5** | Yes, via `chunk_length_schedule` | Per character; we combine them into words | about 75 ms claimed / 182 ms measured | about $40 | Yes (single-use token) |
| **Inworld TTS-2 / Flash** | Yes | Per word, per character, and phonemes | 72 ms (Flash) / 176 ms measured | $15–25 (third-party figure) | Not verified |
| Deepgram Aura-2 | Yes | None found | Not measured | $30 | Not verified |
| Rime | Yes | Estimated from the text, not the audio | Not measured | Not found | No (server proxy only) |
| Azure Neural | Server SDKs only; no SSML | Word boundary events; unconfirmed while text is streaming | Not measured | about $16 | No |
| Google Chirp 3 HD | Yes (preview) | Not documented for streaming | 438 ms measured | $30 | No |
| OpenAI, Amazon Polly, Speechmatics | No, or no timing data | None | — | — | — |

**How reliable these figures are:**
- Measured time to first audio is 2 to 5 times the vendor claims. Budget 200 to 450 ms, plus our own network delay.
- Coval is the only independent source found that measures time to first audio. It changed its method on 2026-06-03, so older and newer numbers cannot be compared.
- Artificial Analysis ranks quality only. Its top two entries are ElevenLabs v4 and Cartesia Sonic 3.6. ElevenLabs v3 and v4 do not accept streamed text on the input-streaming endpoint.
- PlayHT shut down on 2025-12-31 after Meta acquired its team.

**Chunking for the 10-token start:**
- Ten tokens is about 40 to 50 characters.
- With ElevenLabs, set `chunk_length_schedule` to start at 50 (for example `[50,120,200,260]`).
- With Cartesia, keep one `context_id` and set a low `max_buffer_delay_ms`.
- Let the TTS server do its own buffering. Flushing from the client restarts the model and resets prosody.
- The text being read is already known in full, so send it to the TTS a paragraph at a time for the best prosody. The 10-token rule only applies to the agent's spoken answers.

## 3. How TTS works now, and running it on our own server

**Model families:**
- **Classic VITS (Piper):** fast on CPU, but sounds flat.
- **StyleTTS2-style (Kokoro):** very good quality for its size.
- **Flow-matching or diffusion (F5-TTS):** excellent voice cloning, but needs the whole utterance up front.
- **Neural codec language models (Orpheus, Chatterbox, Fish, Qwen3-TTS):** an LLM predicts audio tokens. The most natural prosody, but heavier, and they can skip words.
- **Delayed-streams models (Kyutai TTS, Qwen3-TTS, VibeVoice-Realtime, Dia2):** trained to accept text as it arrives, so they can start from partial text.

Most models need a complete clause or sentence before they start. Only the delayed-streams family can start from a few words.

**Open models worth considering:**

| Model | Stars | License | Accepts streaming text | Word timestamps | Hardware |
|---|---|---|---|---|---|
| **Kokoro-82M** (+ Kokoro-FastAPI, 5.6k stars) | 9.7k | Apache-2.0 | No (sentence at a time) | Yes, through Kokoro-FastAPI | CPU is fine |
| **Kyutai TTS** | 3.0k | CC-BY-4.0 | **Yes** | **Yes, built in** | GPU; claims 16–32 streams per L40S (self-reported) |
| Qwen3-TTS 0.6B / 1.7B | 14.1k | Apache-2.0 | Yes | No | CUDA GPU |
| VibeVoice-Realtime 0.5B | 55.0k (verified) | MIT | Yes | No | GPU. Microsoft removed the larger TTS code in 2025-09 over misuse. |
| Chatterbox | 27.4k | MIT | No | No | Small variant runs on CPU. Every output is watermarked. |
| Kyutai Pocket TTS | 10.1k | MIT code; weights have a voice-cloning restriction | Not documented | No | CPU |

**Avoid for commercial use:**
- F5-TTS, Higgs Audio v3 and XTTS-v2 have non-commercial licences.
- Orpheus and Sesame CSM appear stalled.

**Quality:** closed models hold the top five leaderboard places. The best open model ranks 11th, Kokoro ranks 60th.

**Cost per listening-hour** (about 54,000 characters at 150 words per minute):

| Option | Cost per listening-hour |
|---|---|
| Browser: Web Speech API, or kokoro-js on WebGPU (about 300 MB first download) | $0 |
| Kokoro on CPU, or on a shared L4 GPU running at full load | $0.03–0.05 |
| Kyutai TTS on an L40S | $0.06–0.12 |
| 1–3B codec model on an L4 (estimate) | $0.27–0.80 |
| OpenAI tts-1 / gpt-4o-mini-tts | $0.81–0.90 |
| Google Chirp 3 HD | $1.62 |
| ElevenLabs Flash / Cartesia | $2.05–3.50 |
| ElevenLabs v3 | $4.32 |

**The idle-GPU trap:**
- An always-on L4 costs about $585 a month. It breaks even with ElevenLabs Flash only at about 270 listening-hours a month.
- Serverless GPUs (Modal, RunPod) remove the idle cost but add cold starts of several seconds.

## 4. Barge-in and resume

### Detecting barge-in

- Use layers:
  1. Voice activity detection (Silero; in the browser, `@ricky0123/vad-web`).
  2. Pause playback **immediately**, within 100 to 200 ms.
  3. Commit the interruption only after 2 to 3 transcribed words or more than 400 ms of speech.
  4. Otherwise, un-pause.
- This mirrors LiveKit's `false_interruption_timeout` (2 s) and `resume_false_interruption` settings. Pausing is cheap; cancelling is expensive.
- Backchannel filters, which ignore listener sounds such as "uh-huh" and "okay", exist in LiveKit (cloud only) and Pipecat (Krisp).

### Echo cancellation

- `echoCancellation: true` is required but not sufficient on Chromium.
- Play TTS audio through an `<audio>` element or a WebRTC stream, not straight from an AudioContext.
- Check which echo canceller is active with `track.getSettings()`.
- As a fallback, drop speech-to-text words that fuzzy-match the sentence currently being spoken.
- Headphones remove the problem entirely.

### Tracking the playback position

- ElevenLabs returns `charStartTimesMs` for each chunk; Cartesia returns word timestamps.
- Schedule each audio chunk at a known AudioContext time and keep a map from character offset in the text to scheduled time.
- The position the listener has heard is approximately `getOutputTimestamp().contextTime − outputLatency`.
- Bluetooth output adds 100 to 300 ms. Safari has reported problems with `getOutputTimestamp`.
- **Snap back to the start of the sentence.** This absorbs timing drift and differences between the original and normalized text.

### Cancelling work already in progress

- Abort the LLM request with `AbortController`.
- Cancel the TTS context.
- Stop the audio already queued for playback.
- Tag every message with a generation ID, so audio chunks that arrive after a cancel are discarded.

### Choosing the resume point

- Resume at the start of the interrupted sentence.
- If the listener heard less than about 30% of that sentence, replay the previous sentence as a lead-in.
- Synthesize the audio again from the text rather than reusing cached audio.

### Modelling reading as a tool

- `read_aloud(doc_id, start_offset)` is a client-side tool that does not block.
- When interrupted, it returns a normal `tool_result`:

  ```json
  {"status": "paused", "char_offset": 18234, "sentence_index": 141,
   "last_heard_sentence": "...", "unheard_preview": "...", "user_said": "..."}
  ```

- The model answers the question, which can itself be interrupted without moving the reading cursor. Then it calls `read_aloud(start_offset=sentence_start)`.
- **The application owns the cursor; the model only decides when to resume.**

## 5. Recommended first steps

1. **Read-aloud prototype:**
   - Browser client.
   - ElevenLabs Flash or Cartesia over WebSocket, returning timestamps.
   - Highlight each word as it plays, driven by the audio clock.
   - Pause and resume by button press.
   - *This proves that timestamp-to-text mapping works.*
2. **Add barge-in:** VAD in the browser, pause then confirm, and snap back to the sentence start.
3. **Agent loop:**
   - Claude with the `read_aloud` tool and the paused tool result.
   - The spoken answer starts streaming to TTS after about 10 tokens.
   - Reading resumes afterwards.
4. **Decide on a framework then, not now.** Once steps 1 to 3 have shown us what we need, choose between Pipecat, LiveKit Agents, or keeping our own thin layer. Try Kokoro or Kyutai if hosted TTS costs matter.

## Open items to verify

- Whether Inworld, Deepgram and Hume can be called directly from the browser.
- Pricing for Hume and Rime.
- Azure word boundary events while text is streaming.
- Whether ElevenLabs v4 Turbo returns alignment data.
- Pipecat's behaviour when the TTS provides no word timestamps.
- Real speed of the self-hosted models on our own hardware. The vendor figures are self-reported.

## Key sources

- Pipecat: https://github.com/pipecat-ai/pipecat · https://docs.pipecat.ai/pipecat/fundamentals/interruptions
- LiveKit: https://github.com/livekit/agents · https://docs.livekit.io/reference/agents/turn-handling-options · https://community.livekit.io/t/chat-context-contains-text-that-wasnt-spoken-due-to-interruption/165
- OpenAI Realtime: https://developers.openai.com/api/docs/guides/realtime-conversations
- ElevenLabs input streaming: https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input
- Cartesia WebSocket: https://docs.cartesia.ai/api-reference/tts/websocket
- Inworld timestamps: https://docs.inworld.ai/tts/capabilities/timestamps.md
- Coval time-to-first-audio benchmark: https://www.coval.ai/blog/time-to-first-audio-ttfa-benchmark/
- Artificial Analysis leaderboard: https://artificialanalysis.ai/text-to-speech/leaderboard
- Kyutai TTS: https://github.com/kyutai-labs/delayed-streams-modeling
- Kokoro / Kokoro-FastAPI: https://github.com/hexgrad/kokoro · https://github.com/remsky/Kokoro-FastAPI
- VAD in the browser: https://docs.vad.ricky0123.com/user-guide/api/
- Echo cancellation: https://docs.atmoky.com/known-issues · https://developer.chrome.com/blog/more-native-echo-cancellation/
- Audio output latency: https://web.dev/articles/audio-output-latency
- Claude stop reasons: https://platform.claude.com/docs/en/api/handling-stop-reasons
- Vapi interruption thread: https://support.vapi.ai/t/26740278/how-to-check-where-interruption-was
