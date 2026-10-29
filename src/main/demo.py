"""A scripted stand-in for Claude, so a demo recording is the same every take.

    DEMO=1 uv run uvicorn main.app:app --port 8001

With DEMO set, the app answers from REPLIES below, in order, instead of calling
Bedrock. Everything else is real: Kokoro reads and speaks, Parakeet hears, and the
player, the voice and the cut-ins run as they always do. The replies stream a few
characters at a time, as the model's would.
"""

import itertools
import threading
import time
from types import SimpleNamespace

from anthropic.types import Message

# the arc the README demo shows: read, cut in with a question, answer, carry on
REPLIES = [
    [
        dict(type="text", text="Here is Zarathustra's Prologue, from the beginning."),
        dict(type="tool_use", id="toolu_demo_1", name="player_play", input=dict(chapter=0)),
    ],
    [
        dict(
            type="text",
            text=(
                "He is speaking to the sun. At dawn he greets it like an old friend, and "
                "tells it that it would have no happiness without those it shines on. "
                "Shall I carry on?"
            ),
        )
    ],
    [dict(type="tool_use", id="toolu_demo_2", name="player_resume", input={})],
]
PIECE = 6
# a model writes faster than this, but not instantly
PAUSE = 0.02


def _event(text: str):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


class _Stream:
    def __init__(self, message: Message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for b in self.message.content:
            if b.type == "text":
                for i in range(0, len(b.text), PIECE):
                    time.sleep(PAUSE)
                    yield _event(b.text[i : i + PIECE])

    def get_final_message(self) -> Message:
        return self.message


class Scripted:
    """The model client the app uses in demo mode: replies in order, then silence."""

    def __init__(self, replies=REPLIES):
        self._replies = iter(replies)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def messages(self):
        return self

    def _next(self) -> Message:
        with self._lock:
            blocks = next(self._replies, [dict(type="text", text="That is all for the demo.")])
        return Message.model_validate(
            dict(
                id=f"msg_demo_{next(self._ids)}",
                type="message",
                role="assistant",
                model="demo",
                content=blocks,
                stop_reason="tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
                stop_sequence=None,
                usage=dict(input_tokens=0, output_tokens=0),
            )
        )

    def create(self, **kw) -> Message:
        return self._next()

    def stream(self, **kw) -> _Stream:
        return _Stream(self._next())
