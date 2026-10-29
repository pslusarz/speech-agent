"""Joining Parakeet's subword tokens into timed words, and scoring transcripts."""

import importlib.util
from pathlib import Path

from main.asr import Word, words_of

bench = importlib.util.spec_from_file_location(
    "asr_bench", Path(__file__).resolve().parents[2] / "scripts" / "asr_bench.py"
)


def test_tokens_join_into_words_at_the_spaces():
    tokens = [" Wh", "o", " is", " Zar", "ath", "ustra", "?"]
    times = [0.4, 0.5, 0.7, 0.9, 1.0, 1.1, 1.3]
    assert words_of(tokens, times) == (
        Word("Who", 0.4), Word("is", 0.7), Word("Zarathustra?", 0.9)
    )


def test_a_first_token_without_a_space_still_starts_a_word():
    assert words_of(["Hi", " there"], [0.0, 0.3]) == (Word("Hi", 0.0), Word("there", 0.3))


def test_word_error_rate_ignores_case_and_punctuation():
    m = importlib.util.module_from_spec(bench)
    bench.loader.exec_module(m)
    assert m.wer("Who is Zarathustra?", "who is zarathustra") == 0
    assert m.wer("who does he meet first", "who does he see first") == 0.2
    assert m.wer("who is he", "") == 1.0
