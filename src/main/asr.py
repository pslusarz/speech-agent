"""Speech to text: NVIDIA Parakeet TDT 0.6B v2 (English), int8, on the CPU.

It runs through sherpa-onnx, so it works the same on a Mac and on Linux. The deployment
target, Railway, has no GPUs, and MLX would tie it to Apple. The model comes from
Hugging Face on first use, the way Kokoro's weights do, so it is part of the Python
build and needs no separate download.
"""

import io
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("speech.asr")

REPO = os.getenv("ASR_REPO", "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
RATE = 16000
THREADS = int(os.getenv("ASR_THREADS", 4))
# long enough for a question asked mid-reading, short enough to bound an upload
MAX_SECONDS = 30.0
MIN_SECONDS = 0.3
MAX_BYTES = int(RATE * 2 * MAX_SECONDS) + 1024


class Refused(ValueError):
    """Audio we cannot use; the message says why."""


def check(data: bytes) -> float:
    """The length in seconds of 16 kHz mono 16-bit speech, or Refused with the reason."""
    if len(data) > MAX_BYTES:
        raise Refused(f"too long: at most {MAX_SECONDS:g} seconds")
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = (
                w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            )
    except (wave.Error, EOFError) as e:
        raise Refused(f"not a WAV file: {e}") from e
    if (channels, width, rate) != (1, 2, RATE):
        raise Refused(
            f"expected 16 kHz mono 16-bit audio, got {rate} Hz, {channels} channel(s), "
            f"{width * 8}-bit"
        )
    seconds = frames / RATE
    if seconds < MIN_SECONDS:
        raise Refused("too short to be speech")
    if seconds > MAX_SECONDS:
        raise Refused(f"too long: at most {MAX_SECONDS:g} seconds")
    return seconds


@dataclass(frozen=True)
class Word:
    text: str
    start: float


@dataclass(frozen=True)
class Transcript:
    text: str
    words: tuple[Word, ...]
    # how long the audio was, and how long it took to transcribe
    seconds: float
    took: float


def model_dir(repo: str = REPO) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo, allow_patterns=list(FILES)))


def words_of(tokens: list[str], times: list[float]) -> tuple[Word, ...]:
    """Join subword tokens into words: a token starting with a space begins one."""
    out: list[list] = []
    for tok, t in zip(tokens, times):
        if tok.startswith(" ") or tok.startswith("▁") or not out:
            out.append([tok.lstrip(" ▁"), t])
        else:
            out[-1][0] += tok
    return tuple(Word(w, round(t, 2)) for w, t in out if w)


class Parakeet:
    def __init__(self, threads: int = THREADS, repo: str = REPO):
        import sherpa_onnx

        d = model_dir(repo)
        began = time.monotonic()
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / FILES[0]),
            decoder=str(d / FILES[1]),
            joiner=str(d / FILES[2]),
            tokens=str(d / FILES[3]),
            model_type="nemo_transducer",
            num_threads=threads,
        )
        self._lock = threading.Lock()
        log.info("Parakeet loaded in %.1fs with %d threads", time.monotonic() - began, threads)

    def transcribe(self, samples: np.ndarray, rate: int = RATE) -> Transcript:
        began = time.monotonic()
        with self._lock:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(rate, samples.astype(np.float32))
            self.recognizer.decode_stream(stream)
            r = stream.result
        took = time.monotonic() - began
        words = words_of(list(r.tokens), list(r.timestamps))
        return Transcript(r.text.strip(), words, len(samples) / rate, took)

    def transcribe_wav(self, path: Path) -> Transcript:
        return self.transcribe(*read_wav(path))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    return read_wav_bytes(Path(path).read_bytes())


def read_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit audio")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return pcm.astype(np.float32) / 32768.0, w.getframerate()
