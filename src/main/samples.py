"""Recordings of the person's voice, kept to test speech recognition against.

Each sample is a 16 kHz mono 16-bit WAV, the format the recognisers expect, and,
optionally, what the person says they said, to score a transcript against. They are
personal data: they live in SAMPLES, which git ignores, and nowhere else.
"""

import io
import os
import re
import time
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLES = Path(os.getenv("SAMPLES_PATH", Path(__file__).resolve().parents[2] / "samples"))
RATE = 16000
# long enough for a question asked mid-reading, short enough to bound an upload
MAX_SECONDS = 30.0
MIN_SECONDS = 0.3
MAX_BYTES = int(RATE * 2 * MAX_SECONDS) + 1024
MAX_SAID = 500
NAME = re.compile(r"^\d{8}-\d{6}-\d{3}$")


class Refused(ValueError):
    """An upload that is not a sample we can use; the message says why."""


@dataclass(frozen=True)
class Sample:
    name: str
    seconds: float
    said: str


def check(data: bytes) -> float:
    """The length in seconds of a usable sample, or Refused with the reason."""
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
        raise Refused("too short to be a sample")
    if seconds > MAX_SECONDS:
        raise Refused(f"too long: at most {MAX_SECONDS:g} seconds")
    return seconds


def save(data: bytes, said: str = "", now=time.time) -> Sample:
    seconds = check(data)
    said = " ".join(said.split())[:MAX_SAID]
    t = now()
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{int(t * 1000) % 1000:03d}"
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (SAMPLES / f"{name}.wav").write_bytes(data)
    if said:
        (SAMPLES / f"{name}.txt").write_text(said + "\n")
    return Sample(name, seconds, said)


def path(name: str) -> Path | None:
    """The WAV for a sample's name, if the name is one of ours; never anything else."""
    if not NAME.match(name):
        return None
    p = SAMPLES / f"{name}.wav"
    return p if p.is_file() else None


def all_samples() -> list[Sample]:
    """Every sample, newest first."""
    out = []
    for wav in sorted(SAMPLES.glob("*.wav"), reverse=True) if SAMPLES.is_dir() else []:
        if not NAME.match(wav.stem):
            continue
        with wave.open(str(wav)) as w:
            seconds = w.getnframes() / w.getframerate()
        txt = wav.with_suffix(".txt")
        out.append(Sample(wav.stem, seconds, txt.read_text().strip() if txt.is_file() else ""))
    return out
