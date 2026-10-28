"""The agent and the player together, against a scripted model."""

import json

import pytest

from main.harness.agent import NOTICE, Agent
from main.player.book import Book, chapter
from main.player.player import PAUSED, PLAYING, Player
from main.player.tools import SP, person_did, status_line, tools
from main.player.tts import Silent

from .conftest import Script, last_text, say, use
from .test_player import PARAS, Clock


@pytest.fixture
def player():
    p = Player(
        Book("t", [chapter(0, "One", PARAS), chapter(1, "Two", ["The star rose."])]),
        Silent(),
        clock=Clock(),
        ahead=30.0,
        refill_at=10.0,
        segment_chars=110,
        background=False,
    )
    yield p
    p.stop()


def agent(player, s):
    return Agent(sp=SP, tools=tools(player), client=s, notices=[lambda: status_line(player)])


def sent(s) -> str:
    return json.dumps([r["messages"] for r in s.requests])


def results(a):
    return [b["content"] for e in a.log if e.kind == "tool_results" for b in e.content]


def test_the_agent_starts_the_reading(player):
    s = Script(use("player_play", chapter=0), say("Here it is."))
    a = agent(player, s)
    assert a.wait_for("read me the first chapter", timeout=5).text == "Here it is."
    assert player.state == PLAYING
    assert results(a) == ["playing chapter 0 'One' from offset 0"]
    a.stop()


def test_nothing_about_the_player_is_shown_before_it_is_used(player):
    s = Script(say("Hello."))
    a = agent(player, s)
    a.wait_for("hi", timeout=5)
    assert "The player is" not in sent(s)
    a.stop()


def test_the_state_is_shown_fresh_before_each_turn_and_never_kept(player):
    player.play(0)
    player.fill()
    player.clock.now += 4.0
    s = Script(say("A star."), say("Still going."))
    a = agent(player, s)
    a.wait_for("what is this about?", timeout=5)
    first = last_text(s.requests[0])
    assert first.startswith(NOTICE) and "The player is playing" in first
    assert "Sentence being heard: 'This sentence is exactly fifty characters lo01 ok.'" in first
    player.clock.now += 3.1
    a.wait_for("and now?", timeout=5)
    assert "lo02 ok." in last_text(s.requests[1])
    assert "The player is" not in json.dumps([e.content for e in a.log])
    a.stop()


def test_a_question_while_reading_pauses_answers_and_resumes_where_it_was_asked(player):
    player.play(0)
    player.fill()
    player.clock.now += 4.5  # the second sentence
    s = Script(
        use("player_pause"),
        say("A star is the sun, addressed as a person."),
    )
    a = agent(player, s)
    player.heard_message()
    q = a.post("who is the great star?")
    # the model is slow; the reading carries on into the fourth sentence meanwhile
    player.clock.now += 6.0
    assert a.wait_until(lambda: a.answered(q.id), 5), a.errors
    assert player.state == PAUSED
    assert "When the person last spoke, they were hearing: 'This sentence is exactly fifty characters lo01 ok.'" in last_text(s.requests[0])
    s.then(use("player_resume"), say("Carrying on."))
    a.wait_for("thanks, go on", timeout=5)
    assert player.state == PLAYING
    assert results(a)[-1].startswith("playing again from offset 51,")
    a.stop()


def test_what_the_person_does_on_the_page_is_noted_for_the_agent(player):
    player.play(0, by="person")
    player.fill()
    s = Script(say("You paused it."))
    a = agent(player, s)
    player.pause(by="person")
    a.notify(person_did(player, "paused"))
    a.wait_for("why did it stop?", timeout=5)
    msgs = sent(s)
    assert "The person paused the player in chapter 0 'One'" in msgs
    assert "last changed by the person" in msgs
    # a note asks for no turn of its own
    assert len(s.requests) == 1
    a.stop()


def test_status_gives_the_passage_just_heard(player):
    player.play(0)
    player.fill()
    player.clock.now += 4.0
    s = Script(use("player_status"), say("ok"))
    a = agent(player, s)
    a.wait_for("where are we?", timeout=5)
    out = results(a)[0]
    assert "The passage just heard:" in out
    assert out.endswith("This sentence is exactly fifty characters lo01 ok.")
    a.stop()


def test_the_book_can_be_listed_read_and_searched(player):
    s = Script(
        use("book_contents"),
        use("book_read", chapter=1, start=4),
        use("book_find", query="star"),
        use("book_read", chapter=9),
        say("done"),
    )
    a = agent(player, s)
    a.wait_for("look things up", timeout=5)
    contents, read, found, missing = results(a)
    assert "chapter 1: Two (14 characters)" in contents
    assert read.endswith("star rose.")
    assert "chapter 1 'Two', offset 4: ...The star rose...." in found
    assert missing.startswith("error")
    a.stop()


def test_resume_before_anything_was_played_is_refused(player):
    s = Script(use("player_resume"), say("Nothing to resume."))
    a = agent(player, s)
    a.wait_for("go on", timeout=5)
    assert results(a)[0].startswith("error")
    a.stop()
