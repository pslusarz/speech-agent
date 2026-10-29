"""The page's routes against a silent player and a scripted model, without a browser."""

import json
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
from main.player.voice import Voice

from .conftest import Script, say, use
from .test_player import PARAS, Clock


class Heard:
    """A recogniser that hears whatever the test says it will."""

    def __init__(self, *a, **kw):
        self.next = ""
        self.heard: list[int] = []

    def transcribe(self, pcm, rate=16000):
        from main.asr import Transcript

        self.heard.append(len(pcm))
        return Transcript(self.next, (), len(pcm) / rate, 0.01)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    # main.app builds Kokoro and a Bedrock client when imported; stand both in
    import main.player.tts as tts
    import main.bedrock as bedrock

    import main.asr as asr

    monkeypatch.setenv("LOG_FILE", str(tmp_path / "test.log"))
    monkeypatch.setattr(tts, "Kokoro", Silent)
    monkeypatch.setattr(asr, "Parakeet", Heard)
    monkeypatch.setattr(bedrock, "raw_client", lambda: Script())
    monkeypatch.setattr(Book, "load", classmethod(lambda cls: Book("t", [chapter(0, "One", PARAS)])))
    sys.modules.pop("main.app", None)
    import main.app as app_module

    app_module.agent.stop()
    app_module.voice.close()
    app_module.player.stop()
    player = Player(
        Book("t", [chapter(0, "One", PARAS)]), Silent(), clock=Clock(),
        ahead=30.0, refill_at=10.0, segment_chars=110, background=False,
    )
    s = Script()
    trace = Trace()
    voice = Voice(player, Silent(), clock=player.clock, background=False)
    agent = trace.attach(
        Agent(
            sp=SP, tools=tools(player), client=s, notices=[lambda: status_line(player)],
            on_text=[voice.text], on_end=[voice.end], heard=voice.heard,
        )
    )
    ears = Heard()
    client = TestClient(app_module.make_app(agent, player, trace, voice, ears))
    client.voice = voice
    client.ears = ears
    yield client, player, agent, s
    agent.stop()
    voice.close()
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
    assert "EventSource('/player/events?page=' + PAGE)" in r.text


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


def test_a_reply_is_streamed_in_pieces_then_kept_under_the_same_call():
    s = Script(use("player_status"), say("Thou great star!"))
    trace = Trace()
    player = Player(Book("t", [chapter(0, "One", PARAS)]), Silent(), background=False)
    agent = trace.attach(Agent(tools=tools(player), client=trace.client(s)))
    agent.wait_for("hello", timeout=5)
    # the first call only used a tool, so wrote nothing; the second wrote the reply
    assert {d["call"] for d in trace.live} == {2}
    assert "".join(d["text"] for d in trace.live) == "Thou great star!"
    assert len(trace.live) > 1, "it arrived in pieces"
    said = [st for st in trace.steps if st.said and st.said[0] == "agent"]
    assert [(st.said[1], st.call) for st in said] == [("Thou great star!", 2)]
    assert [st.kind for st in trace.steps].count("response") == 2
    agent.stop()
    player.stop()


def test_speaking_over_a_finished_reply_stops_the_voice_and_says_what_was_missed(setup):
    client, player, agent, s = setup
    voice = client.voice
    s.then(say("Zarathustra went up, alone and gladly, into the mountains. Then he came down."))
    agent.wait_for("why the mountains?", timeout=5)
    assert agent.wait_until(lambda: not agent.busy, 5)
    while voice.pump():
        pass
    first = voice.parts[0]
    client.post("/voice/report", data=dict(id=first.id, t=0.5, playing=0))
    s.then(say("Sure."))
    client.post("/say", data=dict(msg="stop, who is he?"))
    # the next reply may already be queued; the one spoken over is stopped
    assert all(p.over for p in voice.parts if p.call == 1)
    assert any(e["kind"] == "voice-stop" for e in player.events)
    note = [e.text for e in agent.log if e.kind == "note"][-1]
    assert "cut you off while you were speaking" in note and "Then he came down." in note
    assert agent.wait_until(lambda: len(s.requests) == 2, 5)
    msgs = s.requests[1]["messages"]
    # what was never spoken is gone from the history, and from the page
    said = [b["text"] for m in msgs if m["role"] == "assistant" for b in m["content"]]
    heard = voice.heard(first.call, first.text)
    assert said[0] == heard.rstrip() and "came down" not in said[0]
    assert ("agent", heard.rstrip()) in agent.transcript()
    assert "came down" not in client.get("/").text
    sent = json.dumps(msgs[-1])
    assert sent.index("cut you off") < sent.index("stop, who is he?")


def test_voice_audio_is_served_once_made(setup):
    client, player, agent, s = setup
    voice = client.voice
    voice.text(1, "Hello there, and welcome to the book.")
    voice.end(1, None)
    assert client.get("/voice/1").status_code == 404
    voice.pump()
    r = client.get("/voice/1")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"


def test_a_play_held_for_the_voice_also_waits_for_the_turn_to_end(setup):
    client, player, agent, s = setup
    voice = client.voice
    player.hold = lambda: voice.busy or agent.busy
    agent.on_idle.append(player.release)
    # it starts the book, then says something more in the same turn
    hold = s.hold(request=2, after=1)
    s.then(use("player_play", "Here we go.", chapter=0), say("Enjoy the reading!"))
    agent.post("read to me")
    assert hold.reached.wait(5)
    while voice.pump():
        pass
    for p in voice.parts:
        voice.report(p.id, p.duration, playing=False, ended=True)
    # the voice is quiet, but the turn is still writing: the play waits
    assert player.pending is not None and player.state == "idle"
    hold.released.set()
    assert agent.wait_until(lambda: not agent.busy, 5)
    while voice.pump():
        pass
    for p in voice.parts:
        voice.report(p.id, p.duration, playing=False, ended=True)
    assert player.state == "playing" and player.pending is None
    # and nothing paused it after it started
    kinds = [e["kind"] for e in player.events]
    assert "pause" not in kinds[kinds.index("session"):]


def test_the_page_log_reaches_the_server_log(setup, caplog):
    client, *_ = setup
    entries = [dict(page="tab1", kind="audio-play", data=dict(at=1.5)),
               dict(page="tab1", kind="voice-ended", data={})]
    with caplog.at_level("INFO", logger="speech.page"):
        r = client.post("/log", json=dict(entries=entries))
    assert r.status_code == 200
    lines = [rec.getMessage().split(" ")[:2] for rec in caplog.records if rec.name == "speech.page"]
    assert lines == [["tab1", "audio-play"], ["tab1", "voice-ended"]]


def test_a_recorded_sample_is_kept_listed_and_played_back(setup, tmp_path, monkeypatch):
    from main import samples

    from .test_samples import tone

    client, *_ = setup
    monkeypatch.setattr(samples, "SAMPLES", tmp_path / "samples")
    r = client.post("/samples", files=dict(audio=("sample.wav", tone(1.5), "audio/wav")),
                    data=dict(said="who does he meet first?"))
    assert r.status_code == 200 and "who does he meet first?" in r.text
    name = samples.all_samples()[0].name
    assert f"/sample/{name}" in client.get("/").text
    played = client.get(f"/sample/{name}")
    assert played.headers["content-type"] == "audio/wav" and played.content[:4] == b"RIFF"


def test_an_unusable_recording_is_refused_with_the_reason(setup, tmp_path, monkeypatch):
    from main import samples

    from .test_samples import tone

    client, *_ = setup
    monkeypatch.setattr(samples, "SAMPLES", tmp_path / "samples")
    r = client.post("/samples", files=dict(audio=("s.wav", tone(1.0, rate=44100), "audio/wav")))
    assert r.status_code == 400 and "16 kHz mono" in r.text
    assert client.get("/sample/..%2F..%2Fpyproject").status_code == 404


def test_the_microphone_scripts_are_served(setup):
    client, *_ = setup
    for name in ("mic.js", "capture-worklet.js"):
        assert client.get(f"/static/{name}").status_code == 200


def test_the_page_asks_for_each_script_by_its_version(setup):
    import re

    client, *_ = setup
    page = client.get("/").text
    for name in ("player.js", "mic.js", "capture-worklet.js"):
        url = re.search(rf'/static/{re.escape(name)}\?v=\d+', page)
        assert url, f"{name} is not versioned"
        assert client.get(url.group()).status_code == 200


def speak_into(client, text: str, seconds: float = 1.5):
    from .test_samples import tone

    client.ears.next = text
    return client.post("/speech", files=dict(audio=("speech.wav", tone(seconds), "audio/wav")))


def test_pressing_to_speak_cuts_in_and_pauses_the_book_where_the_person_was(setup):
    client, player, agent, s = setup
    player.play(0)
    player.fill()
    player.clock.now += 4.0
    assert client.post("/speech/start").status_code == 200
    assert player.state == "paused" and player.last_by == "person"
    assert player.book[0].sentence_at(player.mark)[0] == 51
    note = [e.text for e in agent.log if e.kind == "note"][-1]
    assert "pressed to speak" in note


def test_pressing_to_speak_over_the_agent_stops_its_voice_and_cuts_its_reply(setup):
    client, player, agent, s = setup
    voice = client.voice
    s.then(say("Zarathustra went up, alone and gladly, into the mountains. Then he came down."))
    agent.wait_for("why the mountains?", timeout=5)
    assert agent.wait_until(lambda: not agent.busy, 5)
    while voice.pump():
        pass
    client.post("/voice/report", data=dict(id=voice.parts[0].id, t=0.5, playing=0))
    client.post("/speech/start")
    assert not voice.busy
    # the reply is cut to what was heard; only the note says what it went on to say
    replies = [e.text for e in agent.log if e.role == "assistant"]
    assert replies and "came down" not in replies[0]


def test_releasing_posts_what_was_heard_as_the_person_s_message(setup):
    client, player, agent, s = setup
    client.post("/speech/start")
    s.then(say("He meets an old saint in the forest."))
    r = speak_into(client, "Who does he meet first?")
    assert r.status_code == 200 and r.json()["text"] == "Who does he meet first?"
    assert agent.wait_until(lambda: len(s.requests) == 1, 5)
    assert ("user", "Who does he meet first?") in agent.transcript()
    assert client.ears.heard == [24000]


def test_speech_with_no_words_in_it_posts_nothing(setup):
    client, player, agent, s = setup
    client.post("/speech/start")
    r = speak_into(client, "   ")
    assert r.status_code == 200 and r.json()["text"] == ""
    assert not [e for e in agent.log if e.kind == "said"]


def test_unusable_speech_audio_is_refused_with_the_reason(setup):
    from .test_samples import tone

    client, *_ = setup
    r = client.post("/speech", files=dict(audio=("s.wav", tone(1.0, rate=44100), "audio/wav")))
    assert r.status_code == 400 and "16 kHz mono" in r.text


def test_the_newest_page_plays_and_the_others_are_told_to_stay_quiet(setup):
    client, player, *_ = setup
    player.claim("page-a")
    player.claim("page-b")
    actives = [e for e in player.events if e["kind"] == "active"]
    assert [e["page"] for e in actives] == ["page-a", "page-b"]
    assert player.active_page == "page-b"


def test_only_the_playing_page_moves_the_book_or_the_voice(setup):
    client, player, agent, s = setup
    voice = client.voice
    player.play(0)
    player.fill()
    player.claim("page-b")
    client.post("/player/report", data=dict(session=player.session, index=1, t=2.0, page="page-a"))
    assert player.position() == (0, 0.0), "a quiet page's report is ignored"
    client.post("/player/report", data=dict(session=player.session, index=1, t=2.0, page="page-b"))
    assert player.position() == (1, 2.0)
    voice.text(1, "Hello there, and welcome to the book.")
    voice.end(1, None)
    voice.pump()
    part = voice.parts[0]
    client.post("/voice/report", data=dict(id=part.id, t=part.duration, ended=1, page="page-a"))
    assert not part.ended
    client.post("/voice/report", data=dict(id=part.id, t=part.duration, ended=1, page="page-b"))
    assert part.ended


def test_a_page_that_does_not_say_which_it_is_gets_no_events(setup):
    client, player, *_ = setup
    r = client.get("/player/events")
    assert r.status_code == 409
    assert player.active_page is None


def test_book_audio_answers_byte_ranges_so_the_browser_can_seek(setup):
    client, player, *_ = setup
    player.play(0)
    player.fill()
    seg = player.segments[0]
    whole = client.get(f"/audio/{seg.id}")
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes"
    part = client.get(f"/audio/{seg.id}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == seg.wav[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(seg.wav)}"
    tail = client.get(f"/audio/{seg.id}", headers={"Range": "bytes=-5"})
    assert tail.status_code == 206 and tail.content == seg.wav[-5:]
    rest = client.get(f"/audio/{seg.id}", headers={"Range": "bytes=40-"})
    assert rest.content == seg.wav[40:]
    beyond = client.get(f"/audio/{seg.id}", headers={"Range": f"bytes={len(seg.wav) + 1}-"})
    assert beyond.status_code == 416
