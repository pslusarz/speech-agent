"""Text to audio, with where each word falls in time.

An engine takes a passage and returns WAV bytes, its length in seconds, and a Word
for each word it said: the word's span in the passage and when it is heard. Kokoro is
the real one; Silent is for tests and pretends every character takes the same time.
"""

import io
import os
import threading
import wave
from dataclasses import dataclass
from typing import Protocol

import numpy as np

RATE = 24000
VOICE = os.getenv("KOKORO_VOICE", "af_heart")
REPO = "hexgrad/Kokoro-82M"


@dataclass(frozen=True)
class Word:
    """A word's span in the text it was spoken from, and when it is heard."""

    start: int
    end: int
    t0: float
    t1: float


@dataclass(frozen=True)
class Speech:
    wav: bytes
    duration: float
    words: tuple[Word, ...]


class Engine(Protocol):
    def speak(self, text: str, voice: str | None = None) -> Speech: ...


def wav(samples: np.ndarray, rate: int = RATE) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def align(text: str, tokens, offset: float = 0.0, at: int = 0) -> tuple[list[Word], int]:
    """Find each timed token in `text`, in order, from `at`.

    The engine may normalise what it was given, so a token that cannot be found close
    by is skipped rather than allowed to drag the cursor off to a later match.
    """
    words = []
    for tok in tokens:
        if tok.start_ts is None or tok.end_ts is None or not tok.text.strip():
            continue
        i = text.find(tok.text, at, at + len(tok.text) + 40)
        if i < 0:
            continue
        at = i + len(tok.text)
        if any(ch.isalnum() for ch in tok.text):
            words.append(Word(i, at, offset + tok.start_ts, offset + tok.end_ts))
    return words, at


class Kokoro:
    """Kokoro-82M on the CPU. One pipeline, used by one thread at a time."""

    def __init__(self, voice: str = VOICE):
        from kokoro import KPipeline

        self.voice = voice
        self.pipeline = KPipeline(lang_code="a", repo_id=REPO)
        self._lock = threading.Lock()

    def speak(self, text: str, voice: str | None = None) -> Speech:
        chunks, words, elapsed, at = [], [], 0.0, 0
        with self._lock:
            # a long passage comes back in several pieces, each timed from its own start
            for r in self.pipeline(text, voice=voice or self.voice):
                audio = r.audio.numpy() if hasattr(r.audio, "numpy") else np.asarray(r.audio)
                found, at = align(text, r.tokens or [], elapsed, at)
                words += found
                chunks.append(audio)
                elapsed += len(audio) / RATE
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return Speech(wav(samples), elapsed, tuple(words))


class Silent:
    """Silence, timed at a fixed number of seconds per character."""

    def __init__(self, per_char: float = 0.06):
        self.per_char = per_char
        self.spoken: list[str] = []

    def speak(self, text: str, voice: str | None = None) -> Speech:
        self.spoken.append(text)
        words, i = [], 0
        for w in text.split():
            i = text.index(w, i)
            words.append(Word(i, i + len(w), i * self.per_char, (i + len(w)) * self.per_char))
            i += len(w)
        duration = len(text) * self.per_char
        # a few samples stand in for the audio, so tests stay fast
        return Speech(wav(np.zeros(8, dtype=np.float32)), duration, tuple(words))
