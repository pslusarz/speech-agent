import copy
import threading
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


def last_text(request: dict) -> str:
    return request["messages"][-1]["content"][-1].get("text", "")
