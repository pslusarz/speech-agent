"""The scripted demo model: the read, ask, answer, carry-on arc, through the real harness."""

from main.demo import REPLIES, Scripted
from main.harness.agent import Agent
from main.player.book import Book, chapter
from main.player.player import PAUSED, PLAYING, Player
from main.player.tools import ENDS_TURN, SP, status_line, tools
from main.player.tts import Silent

from .test_player import PARAS, Clock


def test_the_demo_script_reads_answers_and_carries_on():
    player = Player(Book("t", [chapter(0, "One", PARAS)]), Silent(), clock=Clock(),
                    ahead=30.0, refill_at=10.0, segment_chars=110, background=False)
    a = Agent(sp=SP, tools=tools(player), client=Scripted(), ends_turn=ENDS_TURN,
              notices=[lambda: status_line(player)])
    a.post("Please read me the prologue.")
    assert a.wait_until(lambda: player.state == PLAYING and not a.busy, 5), a.errors
    player.fill()
    player.pause(by="person")
    reply = a.wait_for("Who is he talking to?", timeout=5)
    assert reply.text.startswith("He is speaking to the sun.")
    assert player.state == PAUSED
    a.post("Please carry on.")
    assert a.wait_until(lambda: player.state == PLAYING and not a.busy, 5), a.errors
    assert a.errors == []
    a.stop()
    player.stop()


def test_after_the_script_it_says_so_rather_than_failing():
    s = Scripted(replies=REPLIES[:0])
    with s.stream() as st:
        text = "".join(e.delta.text for e in st)
    assert text == "That is all for the demo."
