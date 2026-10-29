"""The agent's voice against silent audio and a clock the test moves by hand."""

import pytest

from main.player.book import Book, chapter
from main.player.player import PAUSED, PLAYING, Player
from main.player.tts import Silent
from main.player.voice import Chunker, Voice

from .test_player import PARAS, Clock

REPLY = (
    "Zarathustra went up, alone and gladly, into the mountains. There he stayed for ten "
    "years, and did not tire of his solitude. Then his heart changed."
)


def chunks(text, piece=3, **kw):
    c, out = Chunker(**kw), []
    for i in range(0, len(text), piece):
        out += c.feed(text[i : i + piece])
    return out + c.flush()


def test_the_first_chunk_is_the_first_clause_past_about_ten_tokens():
    out = chunks(REPLY, first=30)
    assert out[0] == (0, "Zarathustra went up, alone and gladly, ")
    # then whole sentences
    assert out[1][1] == "into the mountains. "
    assert out[2][1].startswith("There he stayed")
    assert "".join(t for _, t in out) == REPLY
    assert all(REPLY[at : at + len(t)] == t for at, t in out)


def test_a_clause_too_short_to_start_on_waits_for_the_next_break():
    # the first clause ends at 39 characters, under the 40 it needs
    out = chunks(REPLY, first=40)
    assert out[0] == (0, "Zarathustra went up, alone and gladly, into the mountains. ")


def test_a_short_reply_is_one_chunk_on_flush():
    assert chunks("Yes.") == [(0, "Yes.")]


def test_text_with_no_break_is_cut_at_a_space():
    text = "word " * 100
    out = chunks(text, most=60)
    assert all(len(t) <= 60 for _, t in out)
    assert "".join(t for _, t in out) == text


@pytest.fixture
def voice():
    clock = Clock()
    player = Player(
        Book("t", [chapter(0, "One", PARAS)]), Silent(), clock=clock,
        ahead=30.0, refill_at=10.0, segment_chars=110, background=False,
    )
    v = Voice(player, Silent(), clock=clock, background=False)
    yield v, player, clock
    v.close()
    player.stop()


def speak(v, call, text):
    for i in range(0, len(text), 5):
        v.text(call, text[i : i + 5])
    v.end(call, None)
    while v.pump():
        pass


def test_speaking_pauses_the_book(voice):
    v, player, _ = voice
    player.play(0)
    player.fill()
    speak(v, 1, REPLY)
    assert player.state == PAUSED and player.last_by == "agent"
    assert [e["kind"] for e in player.events].count("voice-part") == len(v.parts)


def test_a_resume_waits_for_the_voice_then_happens(voice):
    v, player, _ = voice
    player.play(0)
    player.fill()
    speak(v, 1, "One moment, while I look.")
    player.resume()
    assert player.state == PAUSED and player.pending[0] == "resume"
    for p in v.parts:
        v.report(p.id, p.duration, playing=False, ended=True)
    assert player.state == PLAYING and player.pending is None
    kinds = [e["kind"] for e in player.events]
    assert kinds.index("voice-done") < len(kinds) - 1 and kinds[-1] == "play"


def test_the_book_makes_no_audio_while_the_voice_speaks(voice):
    v, player, _ = voice
    player.play(0)
    speak(v, 1, "Hold on.")
    player.fill()
    assert player.segments == []


def test_heard_follows_the_page_word_by_word(voice):
    v, _, clock = voice
    speak(v, 1, REPLY)
    first, second = v.parts[0], v.parts[1]
    v.report(first.id, 0.0, playing=True)
    # the first word begins at once
    assert v.heard(1, REPLY) == "Zarathustra"
    clock.now += 0.06 * len("Zarathustra went") + 0.01
    assert v.heard(1, REPLY) == "Zarathustra went"
    v.report(first.id, first.duration, playing=False, ended=True)
    v.report(second.id, 0.0, playing=True)
    clock.now += 0.06 * len("There he") + 0.01
    assert v.heard(1, REPLY) == first.text + "There he"


def test_heard_is_nothing_before_the_page_has_played_anything(voice):
    v, _, _ = voice
    speak(v, 1, REPLY)
    assert v.heard(1, REPLY) == ""


def test_stop_drops_everything_and_any_held_resume(voice):
    v, player, _ = voice
    player.play(0)
    player.fill()
    for piece in ("Zarathustra went up, alone and gladly, ", "into the mountains. "):
        v.text(1, piece)
    v.pump()
    player.resume()
    assert v.stop() == {1: ("", "Zarathustra went up, alone and gladly, into the mountains. ")}
    assert not v.busy and v.queue == type(v.queue)()
    assert player.pending is None and player.state == PAUSED
    assert player.events[-1]["kind"] == "voice-stop"
    assert v.stop() == {}, "nothing left to stop"


def test_stop_freezes_what_was_heard_mid_word(voice):
    v, _, clock = voice
    speak(v, 1, REPLY)
    first = v.parts[0]
    v.report(first.id, 0.0, playing=True)
    clock.now += 0.06 * len("Zarathustra went") + 0.01
    out = v.stop()
    assert out[1][0] == "Zarathustra went"
    assert out[1][0] + out[1][1] == REPLY
    # time going on after the stop changes nothing
    clock.now += 100
    assert v.heard(1, REPLY) == "Zarathustra went"


def test_a_reply_already_heard_in_full_is_not_reported(voice):
    v, _, _ = voice
    speak(v, 1, "Yes.")
    v.report(v.parts[0].id, 1.0, playing=False, ended=True)
    speak(v, 2, "And another thing, which goes on.")
    assert set(v.stop()) == {2}


def test_a_part_made_while_stopping_is_dropped(voice):
    v, _, _ = voice
    v.text(1, "Zarathustra went up, alone and gladly, ")
    real = v.engine.speak

    def stop_midway(text, voice=None):
        v.stop()
        return real(text, voice)

    v.engine.speak = stop_midway
    v.pump()
    assert v.parts == []


def test_an_interrupted_call_says_no_more(voice):
    v, _, _ = voice
    v.text(1, "Zarathustra went up, alone and gladly, into the mo")
    v.end(1, "Zarathustra went up")
    assert not v.queue and not v.busy


def test_a_reply_stopped_once_is_not_reported_again(voice):
    v, _, clock = voice
    speak(v, 1, REPLY)
    v.report(v.parts[0].id, 0.5, playing=False)
    assert set(v.stop()) == {1}
    # the agent answers again, and the person cuts in again
    speak(v, 2, "Nietzsche chose him on purpose, and with some irony.")
    v.report(v.parts[-1].id, 0.5, playing=False)
    assert set(v.stop()) == {2}


def test_the_agent_speaks_in_the_book_s_voice_by_default():
    from main.player import tts, voice

    assert voice.VOICE == tts.VOICE
