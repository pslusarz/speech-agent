"""Voice samples: what is kept, what is refused, and that names cannot reach elsewhere."""

import numpy as np
import pytest

from main import samples
from main.player.tts import wav


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(samples, "SAMPLES", tmp_path / "samples")
    return tmp_path / "samples"


def tone(seconds: float, rate: int = 16000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return wav((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), rate)


def test_a_sample_is_kept_with_what_was_said(folder):
    s = samples.save(tone(2.0), "  who is   Zarathustra?\n", now=lambda: 1761700000.123)
    assert s.seconds == pytest.approx(2.0)
    assert s.said == "who is Zarathustra?"
    assert (folder / f"{s.name}.wav").read_bytes()[:4] == b"RIFF"
    assert (folder / f"{s.name}.txt").read_text() == "who is Zarathustra?\n"
    assert samples.all_samples() == [s]
    assert samples.path(s.name) == folder / f"{s.name}.wav"


def test_what_was_said_is_optional(folder):
    s = samples.save(tone(1.0))
    assert s.said == "" and not (folder / f"{s.name}.txt").exists()


def test_newest_comes_first():
    a = samples.save(tone(1.0), now=lambda: 1761700000.0)
    b = samples.save(tone(1.0), now=lambda: 1761700060.0)
    assert [s.name for s in samples.all_samples()] == [b.name, a.name]


@pytest.mark.parametrize(
    "data, why",
    [
        (b"not audio at all", "not a WAV"),
        (tone(1.0, rate=48000), "expected 16 kHz mono"),
        (tone(0.1), "too short"),
        (tone(31.0), "too long"),
    ],
)
def test_unusable_uploads_are_refused_with_a_reason(data, why, folder):
    with pytest.raises(samples.Refused, match=why):
        samples.save(data)
    assert not folder.exists() or not list(folder.iterdir())


def test_names_cannot_reach_outside_the_folder(folder):
    samples.save(tone(1.0))
    for name in ("../secret", "..%2Fsecret", "/etc/passwd", "20261029-120000-000.wav", ""):
        assert samples.path(name) is None
    assert samples.path("20991231-235959-999") is None, "well formed, but no such sample"
