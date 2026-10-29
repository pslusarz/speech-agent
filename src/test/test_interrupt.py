"""Cutting the agent off while it writes: what stops, what is kept, what comes next."""

import json

from main.harness.agent import NOTICE, Agent
from main.harness.tools import Tool

from .conftest import Script, check, cut, say, use

LONG = "Zarathustra went into the mountains because he loved solitude. Then he came down."


def lookup():
    return Tool(
        dict(name="lookup", description="Look it up.", input_schema=dict(type="object", properties={})),
        lambda agent: "found it",
        synchronous=True,
    )


def test_interrupting_mid_stream_keeps_what_was_written_and_stops_the_turn():
    s = Script(say(LONG), say("Of course, go on."))
    hold = s.hold(request=1, after=5)  # 20 characters in
    ended = []
    a = Agent(client=s, on_end=[lambda n, kept: ended.append((n, kept))])
    q = a.post("why did he go?")
    assert hold.reached.wait(5)
    assert cut(a, hold)
    assert not a.busy
    kept = [e for e in a.log if e.role == "assistant"]
    assert [e.text for e in kept] == [LONG[:20]]
    assert ended == [(1, LONG[:20])]
    # the message that interrupted lands after what was kept, and gets its own turn
    a.wait_for("wait, I have a question", timeout=5)
    msgs = s.requests[1]["messages"]
    check(msgs)
    assert msgs[1] == dict(role="assistant", content=[dict(type="text", text=LONG[:20])])
    # in text the person read all that was written, so there is no unheard rest to report
    assert "cut you off" not in json.dumps(msgs)
    assert msgs[2]["content"][-1]["text"] == "wait, I have a question"
    assert q.id < kept[0].id
    a.stop()


def test_what_is_kept_is_what_the_person_heard():
    s = Script(say(LONG), say("ok"))
    hold = s.hold(request=1, after=10)
    a = Agent(client=s, heard=lambda n, text: text[:12])
    a.post("why?")
    assert hold.reached.wait(5)
    cut(a, hold)
    assert [e.text for e in a.log if e.role == "assistant"] == [LONG[:12].rstrip()]
    note = [e.text for e in a.log if e.kind == "note"][0]
    assert note.startswith(NOTICE) and LONG[12:40] in note
    # the note sits between what was kept and whatever the person says next
    a.wait_for("go on", timeout=5)
    last = json.dumps(s.requests[1]["messages"][-1])
    assert last.index("cut you off") < last.index("go on")
    a.stop()


def test_nothing_heard_means_nothing_kept_but_the_note():
    s = Script(say(LONG))
    hold = s.hold(request=1, after=3)
    a = Agent(client=s, heard=lambda n, text: "")
    a.post("why?")
    assert hold.reached.wait(5)
    cut(a, hold)
    assert [e.role for e in a.log] == ["user", "user"]
    assert a.log[1].kind == "note"
    a.stop()


def test_a_turn_between_steps_stops_before_the_next_model_call():
    s = Script(use("lookup"), say(LONG))
    hold = s.hold(request=2, after=1)
    a = Agent(tools=[lookup()], client=s)
    a.post("look it up")
    assert hold.reached.wait(5)
    cut(a, hold)
    # the tool call and its result stand; only the reply being written was cut
    kinds = [(e.role, e.kind) for e in a.log]
    assert kinds[:3] == [("user", "said"), ("assistant", "said"), ("user", "tool_results")]
    assert len(s.requests) == 2
    a.stop()


def test_interrupting_when_nothing_runs_does_nothing():
    s = Script(say("hi"))
    a = Agent(client=s)
    a.wait_for("hello", timeout=5)
    assert a.wait_until(lambda: not a.busy, 5)
    assert not a.interrupt()
    assert [e.text for e in a.log if e.role == "assistant"] == ["hi"]
    a.stop()


def test_a_finished_call_reports_its_end_with_nothing_cut():
    s = Script(say("hi"))
    ended = []
    a = Agent(client=s, on_end=[lambda n, kept: ended.append((n, kept))])
    a.wait_for("hello", timeout=5)
    assert ended == [(1, None)]
    a.stop()


def test_truncate_cuts_a_stored_reply_to_what_was_heard_and_keeps_its_tool_calls():
    s = Script(use("lookup", "Let me look that up for you now."), say("Found it."), say("ok"))
    a = Agent(tools=[lookup()], client=s)
    a.wait_for("look it up", timeout=5)
    first = next(e for e in a.log if e.role == "assistant")
    assert a.truncate(first.call, "Let me look ")
    assert first.text == "Let me look"
    assert [b["type"] for b in first.content] == ["text", "tool_use"]
    second = [e for e in a.log if e.role == "assistant"][1]
    assert a.truncate(second.call, "")
    assert second.content == []
    a.wait_for("and?", timeout=5)
    msgs = s.requests[-1]["messages"]
    check(msgs)
    assert "Found it." not in json.dumps(msgs)
    assert not a.truncate(999, "x")
    a.stop()
