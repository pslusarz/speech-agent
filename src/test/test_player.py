"""The player against silent audio and a clock the test moves by hand."""

import pytest

from main.player.book import Book, chapter
from main.player.player import FINISHED, PAUSED, PLAYING, Player
from main.player.tts import Silent

# each sentence is 50 characters, so 3 seconds of audio at 0.06 s a character
S = "This sentence is exactly fifty characters long ok."
assert len(S) == 50
PARAS = [" ".join(f"{S[:-6]}{i:02d} ok." for i in range(p * 10, p * 10 + 10)) for p in range(6)]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def make():
    made = []

    def _make(**kw):
        clock = Clock()
        engine = Silent()
        kw = dict(ahead=30.0, refill_at=10.0, segment_chars=110, background=False) | kw
        p = Player(Book("t", [chapter(0, "One", PARAS)]), engine, clock=clock, **kw)
        made.append(p)
        return p, clock, engine

    yield _make
    for p in made:
        p.stop()


def test_segments_are_whole_sentences_within_one_paragraph(make):
    p, _, engine = make()
    p.play(0)
    p.fill()
    c = p.book[0]
    for s in p.segments:
        assert c.text[s.start:s.end] in engine.spoken
        assert c.paragraph_at(s.start) == c.paragraph_at(s.end - 1)
    # two 50-character sentences and a space fit in 110
    assert [s.end - s.start for s in p.segments[:2]] == [101, 101]


def test_it_prepares_up_to_ahead_and_no_further(make):
    p, _, _ = make()
    p.play(0)
    p.fill()
    # 101 characters, 6.06 s a segment: the fifth takes it past 30 s
    assert len(p.segments) == 5
    assert p.ready_ahead() == pytest.approx(5 * 101 * 0.06)


def test_it_refills_only_once_less_than_refill_at_is_left(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 19.0
    p.fill()
    assert len(p.segments) == 5, "11 s are still ready, above the 10 s threshold"
    clock.now += 2.0
    p.fill()
    assert p.ready_ahead() >= 30.0
    assert len(p.segments) == 9


def test_the_word_heard_follows_the_clock(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    c = p.book[0]
    assert p.offset() == 0
    # the second sentence starts at character 51, 3.06 s in
    clock.now += 3.1
    s0, _ = c.sentence_at(p.offset())
    assert s0 == 51
    # crossing into the next segment
    clock.now += 3.0
    assert p.position()[0] == 1


def test_pause_freezes_the_position(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 4.0
    assert p.pause(by="person")
    at = p.offset()
    clock.now += 100.0
    assert p.offset() == at
    assert p.status()["state"] == PAUSED
    assert p.status()["by"] == "person"


def test_resume_goes_back_to_the_start_of_the_sentence(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 4.5  # into the second sentence
    p.pause()
    start = p.resume()
    assert start == 51
    assert p.state == PLAYING
    assert p.position() == (0, pytest.approx(51 * 0.06))


def test_resume_returns_to_where_the_person_spoke_not_where_it_paused(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 4.5
    p.heard_message()
    # the agent takes a while to pause; reading has moved on two sentences
    clock.now += 6.0
    p.pause()
    assert p.resume() == 51


def test_resume_beyond_what_is_prepared_starts_a_new_session(make):
    p, _, _ = make()
    p.play(0)
    p.fill()
    session = p.session
    c = p.book[0]
    far = c.paragraphs[5][0] + 10
    start = p.resume(far)
    assert start == c.paragraphs[5][0]
    assert p.session == session + 1
    assert p.segments == []


def test_a_report_from_the_page_corrects_the_clock(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 50.0
    p.report(p.session, 0, 1.0)
    assert p.position() == (0, 1.0)


def test_reports_from_an_old_session_are_ignored(make):
    p, _, _ = make()
    p.play(0)
    p.fill()
    old = p.session
    p.play(0, 200)
    p.fill()
    p.report(old, 3, 2.0)
    assert p.position() == (0, 0.0)


def test_a_segment_made_for_an_old_session_is_dropped():
    clock = Clock()

    class Interrupting(Silent):
        def speak(self, text):
            if not self.spoken:
                player.play(0, 300)
            return super().speak(text)

    player = Player(
        Book("t", [chapter(0, "One", PARAS)]), Interrupting(), clock=clock,
        ahead=6.0, refill_at=1.0, segment_chars=110, background=False,
    )
    player.play(0)
    player.fill()
    assert all(s.session == player.session for s in player.segments)
    assert player.segments[0].start == player.book[0].sentence_at(300)[0]


def test_it_finishes_when_the_last_segment_has_been_played(make):
    p, _, _ = make(ahead=10_000.0)
    p.play(0)
    p.fill()
    assert p.next_offset is None
    last = len(p.segments) - 1
    p.report(p.session, last, p.segments[last].duration)
    assert p.state == FINISHED
    assert p.events[-1]["kind"] == "finished"


def test_status_and_tail_describe_what_is_being_heard(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 4.0
    p.heard_message()
    st = p.status()
    assert st["state"] == PLAYING
    assert st["title"] == "One"
    assert st["sentence"] == p.book[0].text[51:101]
    assert st["mark_sentence"] == st["sentence"]
    assert p.tail().endswith(st["sentence"])
    assert p.tail().startswith(p.book[0].text[:10])


def test_a_snapshot_lets_a_new_page_pick_up_mid_reading(make):
    p, clock, _ = make()
    p.play(0)
    p.fill()
    clock.now += 7.0
    snap = p.snapshot()
    assert (snap["state"], snap["chapter"], snap["index"]) == (PLAYING, 0, 1)
    assert snap["t"] == pytest.approx(7.0 - 101 * 0.06)
    assert [s["id"] for s in snap["segments"]] == [s.id for s in p.segments]
    assert snap["n"] == len(p.events)


def test_events_tell_the_page_what_to_play(make):
    p, _, _ = make()
    p.play(0, by="person")
    p.fill()
    kinds = [e["kind"] for e in p.events]
    assert kinds[0] == "session" and set(kinds[1:]) == {"segment"}
    assert p.events[1]["words"][0] == [0, 4, 0.0, pytest.approx(0.24)]
    assert all(e["session"] == p.session for e in p.events)
