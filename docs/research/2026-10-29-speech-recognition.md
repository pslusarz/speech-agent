# Speech recognition for barge-in: NVIDIA models and alternatives

Date: 2026-10-29. Hugging Face and GitHub figures were read from the pages on this date.
Accuracy figures marked "NVIDIA" are NVIDIA's own; no independent Polish benchmark
was found for any model.

## Decision (with the user)

- **English only, for now.** Kokoro, the reader's voice, has no Polish, so the
  conversation is in English too.
- **Server-side recognition on CPU.** The app is to be deployed on Railway, which offers
  no GPUs (its CEO, May 2026: planned but not offered). So:
  - `parakeet-mlx` is out: it needs Apple's MLX and is Mac-only.
  - The candidate is **Parakeet TDT 0.6B v2 (English) as int8 ONNX through
    `sherpa-onnx`**. It runs the same on the Mac and on Linux.
  - It still has to be benchmarked on the user's voice.
- **Press-to-speak first, automatic voice detection second.** Both feed the same
  `speech-start` / `speech-end` path.

## Does NVIDIA have a model that both recognises and generates speech?

No model that does both as separate services. "Nemotron Speech" is a family of separate
models, shipped through NeMo and the Riva/NIM containers:
- recognition: Parakeet, Canary and Nemotron ASR;
- generation: Magpie TTS;
- diarization: Sortformer.

The one model that listens and speaks is **PersonaPlex-7B-v1** (January 2026), a
full-duplex speech-to-speech model built on Kyutai's Moshi. It replaces the
recognition, LLM and TTS chain, and so would replace Claude too. It is also
English-only, gated, and needs an A100/H100. It does not fit this design.

## NVIDIA models

| Model | Role / size | Licence | Languages | Streaming | Word timestamps | Accuracy | Off-GPU runtimes |
|---|---|---|---|---|---|---|---|
| parakeet-tdt-0.6b-v2 | Recognition, 0.6B | CC-BY-4.0 | English | Chunked | Yes | Open ASR 6.05% WER (leaderboard) | ONNX, MLX |
| parakeet-tdt-0.6b-v3 | Recognition, 0.6B | CC-BY-4.0 (NVIDIA licence on build.nvidia.com) | 25 EU languages, including Polish | Chunked | Yes | Polish about 7.3% (NVIDIA) | parakeet-mlx, CoreML, sherpa-onnx int8 |
| nemotron-speech-streaming-en-0.6b | Cache-aware streaming recognition | NVIDIA Open Model | English | **True streaming**, 80–1120 ms chunks | Not on the card | 8.43% at 80 ms, 6.93% at 1.12 s (NVIDIA); 8.20% for Microsoft Research's int4 CPU port | ONNX int4/int8 on CPU |
| nemotron-3.5-asr-streaming-0.6b | Same design | OpenMDW-1.1 | 40 locales | True streaming | Not stated | Polish 15–20% (NVIDIA) | community ports |
| parakeet_realtime_eou_120m-v1 | Streaming recognition with an end-of-utterance token | NVIDIA Open Model | English | Yes | — | 9.30%; end-of-utterance 160 ms median (NVIDIA, synthetic audio) | GPU listed |
| canary-1b-v2 | Recognition and translation, 1B | CC-BY-4.0 | 25 languages | Offline | Yes | — | unconfirmed |
| magpie_tts_multilingual_357m | Text-to-speech | NVIDIA Open Model | 12 languages, no Polish | — | — | — | GPU listed |
| personaplex-7b-v1 | Full-duplex speech-to-speech | NVIDIA Open Model, gated | English | Yes | — | — | A100/H100 |

**GitHub:**
- NVIDIA-NeMo/NeMo has 18.6k stars. Its README recommends CUDA and says nothing about macOS.
- senstella/parakeet-mlx has 990 stars.

## Alternatives

| Option | Streaming | Word timestamps | Cost / licence | Notes |
|---|---|---|---|---|
| Whisper / faster-whisper / whisper.cpp | No (chunked) | Yes | MIT | Slower |
| Moonshine v2 | Yes | Reports conflict | MIT for English | |
| Kyutai STT 1B | Yes, 0.5 s delay | Yes | CC-BY-4.0 | English and French; semantic VAD in its Rust server |
| Deepgram Flux | Yes | Unverified | About $0.0065–0.0078/min | Best turn events (StartOfTurn, EndOfTurn, EagerEndOfTurn) |
| AssemblyAI Universal-Streaming | Yes | Yes | $0.15–0.45/hr | Endpointing model |
| ElevenLabs Scribe v2 Realtime | Yes | Yes | $0.39/hr | About 150 ms (vendor figure) |
| OpenAI realtime transcription | Yes | Limited | About $0.017/min | `semantic_vad` |

## Open questions

- No measured latency on an M3 or on Railway CPU for any of these models.
- Word timestamps from the Nemotron streaming models are unconfirmed.
- Parakeet's licence is listed differently on Hugging Face and on build.nvidia.com.
- Several hosted prices come from third-party aggregators.

## Sources

- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2
- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3
- https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b
- https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b
- https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1
- https://huggingface.co/nvidia/canary-1b-v2
- https://huggingface.co/nvidia/magpie_tts_multilingual_357m
- https://huggingface.co/nvidia/personaplex-7b-v1
- https://github.com/NVIDIA-NeMo/NeMo
- https://github.com/senstella/parakeet-mlx
- https://docs.nvidia.com/nemo/labs-voice-agent/about/core-concepts/speech-pipeline/speech-recognition
- https://www.microsoft.com/en-us/research/?p=1169047
- https://kyutai.org/stt
- https://elevenlabs.io/pricing/api
- https://www.assemblyai.com/pricing.md
- https://station.railway.com/community/gpu-support-vllm-b92b5375 (Railway GPUs)
