"""Real Kokoro and real Claude on Bedrock, the page left out. Run with -m live.

    LLM_CACHE=off uv run pytest -m live -s src/test/test_live.py
"""

import pytest

from main.bedrock import raw_client
from main.harness.agent import Agent
from main.player.book import Book
from main.player.player import PAUSED, PLAYING, Player
from main.player.tools import SP, status_line, tools
from main.player.tts import Kokoro

pytestmark = pytest.mark.live


def test_kokoro_times_the_words_of_a_real_paragraph():
    book = Book.load()
    c = book[0]
    s, e = c.sentences[0]
    speech = Kokoro().speak(c.text[s:e])
    assert speech.wav[:4] == b"RIFF" and speech.duration > 3
    said = [c.text[w.start:w.end] for w in speech.words]
    assert said[:4] == ["When", "Zarathustra", "was", "thirty"]
    assert all(a.t0 <= b.t0 for a, b in zip(speech.words, speech.words[1:]))


def said_since(a, n: int) -> list[str]:
    return [t for who, t in a.transcript()[n:] if who == "agent"]


def test_claude_reads_pauses_for_a_question_and_resumes_where_it_was_asked():
    book = Book.load()
    player = Player(book, Kokoro(), ahead=40.0, refill_at=10.0)
    a = Agent(sp=SP, tools=tools(player), client=raw_client(), notices=[lambda: status_line(player)])
    try:
        # a reply can come before the tools it goes on to call, so wait on the player
        a.post("Please read me Zarathustra's Prologue.")
        assert a.wait_until(lambda: player.state == PLAYING, 120), a.errors
        assert player.chapter == 0
        assert player.wait_until_ready(timeout=60)
        # as if the page had played the first sentence and a bit of the second
        first = player.segments[0]
        player.report(player.session, 0, first.time_of(c1 := book[0].sentences[1][0]) + 1.0)
        player.heard_message()
        n = len(a.transcript())
        a.post("Wait - why did he go into the mountains?")
        assert a.wait_until(lambda: player.state == PAUSED and said_since(a, n), 120), a.errors
        # the answer may come in the same turn as the resume, or wait to be asked
        a.wait_until(lambda: player.state == PLAYING, 15)
        if player.state != PLAYING:
            a.post("Thanks, please carry on.")
            assert a.wait_until(lambda: player.state == PLAYING, 120), a.errors
        for line in a.transcript():
            print(*line, sep=": ")
        assert book[0].sentence_at(player.offset())[0] == c1
    finally:
        a.stop()
        player.stop()
