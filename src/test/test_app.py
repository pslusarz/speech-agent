"""The page's routes against a silent player and a scripted model, without a browser."""

import sys
import types

import pytest
from starlette.testclient import TestClient

from main.harness.agent import Agent
from main.harness.trace import Trace
from main.player.book import Book, chapter
from main.player.player import PAUSED, PLAYING, Player
from main.player.tools import SP, status_line, tools
from main.player.tts import Silent

from .conftest import Script, say
from .test_player import PARAS, Clock


@pytest.fixture
def setup(monkeypatch, tmp_path):
    # main.app builds Kokoro and a Bedrock client when imported; stand both in
    import main.player.tts as tts
    import main.bedrock as bedrock

    monkeypatch.setenv("LOG_FILE", str(tmp_path / "test.log"))
    monkeypatch.setattr(tts, "Kokoro", Silent)
    monkeypatch.setattr(bedrock, "raw_client", lambda: Script())
    monkeypatch.setattr(Book, "load", classmethod(lambda cls: Book("t", [chapter(0, "One", PARAS)])))
    sys.modules.pop("main.app", None)
    import main.app as app_module

    app_module.agent.stop()
    app_module.player.stop()
    player = Player(
        Book("t", [chapter(0, "One", PARAS)]), Silent(), clock=Clock(),
        ahead=30.0, refill_at=10.0, segment_chars=110, background=False,
    )
    s = Script()
    trace = Trace()
    agent = trace.attach(
        Agent(sp=SP, tools=tools(player), client=s, notices=[lambda: status_line(player)])
    )
    client = TestClient(app_module.make_app(agent, player, trace))
    yield client, player, agent, s
    agent.stop()
    player.stop()
    sys.modules.pop("main.app", None)


def test_the_page_lists_the_chapters(setup):
    client, *_ = setup
    r = client.get("/")
    assert r.status_code == 200
    assert "0. One" in r.text


def test_the_player_script_is_served(setup):
    client, *_ = setup
    r = client.get("/static/player.js")
    assert r.status_code == 200
    assert "EventSource('/player/events')" in r.text


def test_the_buttons_drive_the_player_and_tell_the_agent(setup):
    client, player, agent, _ = setup
    assert client.post("/player/play", data=dict(chapter=0)).status_code == 200
    assert player.state == PLAYING and player.last_by == "person"
    player.fill()
    client.post("/player/pause")
    assert player.state == PAUSED
    client.post("/player/resume")
    assert player.state == PLAYING
    notes = [e.text for e in agent.log if e.kind == "note"]
    assert [n.split("The person ")[1].split(" the player")[0] for n in notes] == [
        "started", "paused", "resumed"
    ]


def test_bad_input_is_refused(setup):
    client, player, *_ = setup
    assert client.post("/player/play", data=dict(chapter=7)).status_code == 400
    assert client.post("/player/resume").status_code == 400
    assert client.get("/chapter/7").status_code == 404
    assert client.get("/audio/99").status_code == 404
    assert player.state == "idle"


def test_audio_and_text_are_served(setup):
    client, player, *_ = setup
    player.play(0)
    player.fill()
    seg = player.segments[0]
    r = client.get(f"/audio/{seg.id}")
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    html = client.get("/chapter/0").text
    assert html.startswith('<p><span data-s="0">This</span>')


def test_a_report_moves_the_player(setup):
    client, player, *_ = setup
    player.play(0)
    player.fill()
    client.post("/player/report", data=dict(session=player.session, index=1, t=2.0))
    assert player.position() == (1, 2.0)


def test_a_message_marks_where_the_person_was(setup):
    client, player, agent, s = setup
    player.play(0)
    player.fill()
    player.clock.now += 4.0
    s.then(say("ok"))
    client.post("/say", data=dict(msg="wait, who is speaking?"))
    assert player.book[0].sentence_at(player.mark)[0] == 51
    assert agent.wait_until(lambda: len(s.requests) == 1, 5)
