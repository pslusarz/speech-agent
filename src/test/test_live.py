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


def test_a_bedrock_stream_can_be_cut_off_from_another_thread():
    import threading
    import time

    pieces, first = [], threading.Event()

    def got(n, text):
        pieces.append((time.monotonic(), text))
        if sum(len(t) for _, t in pieces) > 80:
            first.set()

    a = Agent(client=raw_client(), on_text=[got], maxtok=2000)
    try:
        a.post("Tell me, in about 400 words, the story of Zarathustra's prologue.")
        assert first.wait(60), a.errors
        cut_at = time.monotonic()
        assert a.interrupt(timeout=10)
        stopped = time.monotonic() - cut_at
        written = "".join(t for _, t in pieces)
        late = [t for at, t in pieces if at > cut_at]
        kept = [e.text for e in a.log if e.role == "assistant"]
        print(f"\nstopped in {stopped * 1000:.0f} ms; {len(written)} chars written, "
              f"{len(late)} pieces after the cut; kept {len(kept[0])} chars")
        assert stopped < 2.0
        assert not late
        assert kept == [written.rstrip()]
        reply = a.wait_for("Sorry - in one sentence, who is Zarathustra?", timeout=60).text
        print("next reply:", reply)
        assert a.errors == []
    finally:
        a.stop()


def test_the_voice_starts_soon_after_the_model_does_and_stops_when_cut_in():
    import threading
    import time

    from main.player.voice import Voice

    book = Book.load()
    engine = Kokoro()
    player = Player(book, engine)
    voice = Voice(player, engine)
    marks, first_text, first_part = {}, threading.Event(), threading.Event()

    def got(n, text):
        marks.setdefault("text", time.monotonic())
        first_text.set()

    a = Agent(sp=SP, client=raw_client(), tools=tools(player), maxtok=1500,
              on_text=[got, voice.text], on_end=[voice.end], heard=voice.heard,
              notices=[lambda: status_line(player)])
    try:
        posted = time.monotonic()
        a.post("Without looking anything up, tell me in about 200 words what the book is about.")
        assert first_text.wait(60), a.errors
        seen = 0
        while not any(e["kind"] == "voice-part" for e in player.events):
            seen = player.wait(seen, timeout=30)
        part = next(e for e in player.events if e["kind"] == "voice-part")
        marks["part"] = time.monotonic()
        print(f"\nfirst text {marks['text'] - posted:.2f}s after posting; first audio "
              f"{marks['part'] - marks['text']:.2f}s after that: {part['text']!r} "
              f"({part['duration']:.1f}s)")
        # as if the page had played two seconds of the first part
        voice.report(part["id"], 0.0, playing=True)
        time.sleep(2.0)
        unheard = voice.stop()
        if a.busy:
            a.interrupt()
        heard, rest = unheard[min(unheard)]
        print(f"heard {heard!r}; not heard {len(rest)} chars")
        assert heard and part["text"].startswith(heard.split(" ")[0])
        kept = [e.text for e in a.log if e.role == "assistant"]
        print("kept:", kept)
        assert not voice.busy
    finally:
        a.stop()
        voice.close()
        player.stop()
