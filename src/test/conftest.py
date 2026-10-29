import copy
import threading
import time
from types import SimpleNamespace
from itertools import count

from anthropic.types import Message

_ids = count(1)


def say(text: str) -> list[dict]:
    return [dict(type="text", text=text)]


def use(name: str, text: str = "", **input) -> list[dict]:
    return (say(text) if text else []) + [
        dict(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=input)
    ]


def check(messages: list[dict]):
    """What the API would refuse: roles out of turn, or a tool_use left unanswered."""
    assert messages and messages[0]["role"] == "user"
    assert messages[-1]["role"] == "user", "the model would be asked to continue itself"
    for a, b in zip(messages, messages[1:]):
        assert a["role"] != b["role"], "consecutive messages share a role"
    for i, m in enumerate(messages):
        assert m["content"], "empty message"
        uses = [c["id"] for c in m["content"] if c["type"] == "tool_use"]
        if uses:
            answered = [
                c.get("tool_use_id") for c in messages[i + 1]["content"][: len(uses)]
            ]
            assert answered == uses, "tool_result must lead the next turn"


class Script:
    """A model that says what the test wrote, in order, and keeps every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self._holds: dict[int, "Hold"] = {}
        self._lock = threading.Lock()

    @property
    def messages(self):
        return self

    def then(self, *replies):
        with self._lock:
            self.replies += replies

    def create(self, **kw):
        check(kw["messages"])
        with self._lock:
            self.requests.append(copy.deepcopy(kw))
            if not self.replies:
                raise AssertionError(f"script ran out at request {len(self.requests)}")
            blocks = self.replies.pop(0)
        return Message.model_validate(
            dict(
                id="msg",
                type="message",
                role="assistant",
                model="scripted",
                content=blocks,
                stop_reason="tool_use"
                if any(b["type"] == "tool_use" for b in blocks)
                else "end_turn",
                stop_sequence=None,
                usage=dict(input_tokens=1, output_tokens=1),
            )
        )

    def stream(self, **kw):
        with self._lock:
            hold = self._holds.pop(len(self.requests) + 1, None)
        return Streamed(self.create(**kw), hold)

    def hold(self, request: int, after: int) -> "Hold":
        """Stop the stream for `request` after `after` pieces until released or closed."""
        h = Hold(after)
        with self._lock:
            self._holds[request] = h
        return h


class Hold:
    def __init__(self, after: int):
        self.after = after
        self.reached = threading.Event()
        self.released = threading.Event()


def event(text: str):
    """A text delta, shaped like the SDK's stream event."""
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


class Streamed:
    """What messages.stream() gives back: events carrying the text a few characters at a
    time, then the message.

    With a hold, it stops partway as a slow model would, until the test releases it.
    """

    PIECE = 4

    def __init__(self, message: Message, hold: Hold | None = None):
        self.message = message
        self.hold = hold

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        sent = 0
        for b in self.message.content:
            if b.type != "text":
                continue
            for i in range(0, len(b.text), self.PIECE):
                if self.hold and sent == self.hold.after:
                    self.hold.reached.set()
                    self.hold.released.wait(5)
                sent += 1
                yield event(b.text[i : i + self.PIECE])

    def get_final_message(self) -> Message:
        return self.message


def cut(agent, hold: Hold) -> bool:
    """Interrupt while the stream is held, then let the next event through, as it would
    arrive from a real model a moment later."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("ok", agent.interrupt(timeout=5)))
    t.start()
    while not agent._interrupting.is_set() and t.is_alive():
        time.sleep(0.005)
    hold.released.set()
    t.join(5)
    return out.get("ok", False)


def last_text(request: dict) -> str:
    return request["messages"][-1]["content"][-1].get("text", "")
